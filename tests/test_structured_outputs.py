import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"Constant {name} not found in {path.name}")


class StructuredOutputSchemaTests(unittest.TestCase):
    def test_news_schema_is_strict_and_closed(self):
        schema = load_constant(ROOT / "news_engine.py", "NEWS_SCHEMA")
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_pulse_schema_is_strict_and_closed(self):
        schema = load_constant(ROOT / "pulse_engine.py", "PULSE_SCHEMA")
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_structured_output_configuration_is_present(self):
        for filename in ("news_engine.py", "pulse_engine.py"):
            text = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn('"type": "json_schema"', text)
            self.assertIn('"strict": True', text)
            self.assertIn('"text": {"format":', text)


if __name__ == "__main__":
    unittest.main()
