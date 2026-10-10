import json
import logging
import sys
import unittest

from sportapp_platform.correlation import correlation_id_var
from sportapp_platform.logging import JsonFormatter, configure_logging


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class JsonFormatterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = JsonFormatter(service="probe", environment="test", version="1.2.3")

    def test_includes_static_fields_and_correlation(self) -> None:
        token = correlation_id_var.set("c0ffee00-0000-4000-8000-000000000000")
        try:
            line = json.loads(self.formatter.format(_record("evento")))
        finally:
            correlation_id_var.reset(token)

        self.assertEqual(line["message"], "evento")
        self.assertEqual(line["service"], "probe")
        self.assertEqual(line["environment"], "test")
        self.assertEqual(line["version"], "1.2.3")
        self.assertEqual(line["correlation_id"], "c0ffee00-0000-4000-8000-000000000000")

    def test_includes_only_explicit_fields(self) -> None:
        line = json.loads(
            self.formatter.format(_record("evento", fields={"route": "/x"}, body="no-visible"))
        )

        self.assertEqual(line["route"], "/x")
        self.assertNotIn("body", line)

    def test_serializes_exception(self) -> None:
        try:
            raise ValueError("fallo")
        except ValueError:
            record = _record("error", exc_info=sys.exc_info())

        line = json.loads(self.formatter.format(record))

        self.assertIn("ValueError", line["exception"])


class ConfigureLoggingTest(unittest.TestCase):
    def test_installs_single_json_handler(self) -> None:
        root = logging.getLogger()
        previous_handlers, previous_level = root.handlers, root.level
        try:
            configure_logging(service="probe", environment="test", version="1", level="WARNING")

            self.assertEqual(len(root.handlers), 1)
            self.assertIsInstance(root.handlers[0].formatter, JsonFormatter)
            self.assertEqual(root.level, logging.WARNING)
            self.assertTrue(logging.getLogger("uvicorn.access").disabled)
        finally:
            root.handlers, root.level = previous_handlers, previous_level


if __name__ == "__main__":
    unittest.main()
