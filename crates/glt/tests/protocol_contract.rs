use std::fmt;
use std::path::{Path, PathBuf};

use glt::protocol::{
    ValidationError, validate_events_value, validate_note_sequence_value, validate_report_value,
    validate_worker_value,
};
use serde::Deserialize;
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture_root() -> PathBuf {
    repo_root().join("tests/fixtures/protocol/v1")
}

fn schema_root() -> PathBuf {
    repo_root().join("schemas")
}

fn load_regular_json(path: &Path) -> Value {
    let text = std::fs::read_to_string(path).expect("fixture must be readable");
    serde_json::from_str(&text).expect("fixture must be valid JSON")
}

fn semantic_result(schema_file: &str, document: &Value) -> Result<(), ValidationError> {
    match schema_file {
        "events-v1.schema.json" => validate_events_value(document),
        "worker-v1.schema.json" => validate_worker_value(document),
        "note-sequence-v1.schema.json" => validate_note_sequence_value(document),
        "report-v1.schema.json" => validate_report_value(document),
        other => panic!("unknown schema in fixture manifest: {other}"),
    }
}

#[test]
fn shared_protocol_cases_match_schema_and_semantics() {
    let manifest = load_regular_json(&fixture_root().join("cases.json"));
    let cases = manifest["cases"]
        .as_array()
        .expect("cases must be an array");
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let schema_file = case["schema_file"].as_str().expect("schema file");
        let schema = load_regular_json(&schema_root().join(schema_file));
        let validator = jsonschema::validator_for(&schema).expect("schema must compile");
        let document = if let Some(document) = case.get("document") {
            document.clone()
        } else {
            load_regular_json(&fixture_root().join(case["fixture"].as_str().unwrap()))
        };
        assert_eq!(
            validator.is_valid(&document),
            case["schema_valid"].as_bool().unwrap(),
            "schema expectation failed for {name}"
        );

        let Some(expected_semantic) = case["semantic_valid"].as_bool() else {
            continue;
        };
        let result = semantic_result(schema_file, &document);
        if expected_semantic {
            assert!(
                result.is_ok(),
                "semantic validation failed for {name}: {result:?}"
            );
        } else {
            let error = result.expect_err("semantic validation should fail");
            assert_eq!(
                error.code(),
                case["expected_error"].as_str().unwrap(),
                "semantic error code mismatch for {name}"
            );
        }
    }
}

#[test]
fn raw_json_cases_are_rejected() {
    let manifest = load_regular_json(&fixture_root().join("cases.json"));
    let cases = manifest["raw_json_cases"]
        .as_array()
        .expect("raw cases must be an array");
    for case in cases {
        let path = fixture_root().join(case["fixture"].as_str().unwrap());
        let text = std::fs::read_to_string(path).expect("raw fixture must be readable");
        assert!(
            serde_json::from_str::<StrictValue>(&text).is_err(),
            "raw JSON case unexpectedly parsed: {}",
            case["name"].as_str().unwrap()
        );
    }
}

#[derive(Debug)]
struct StrictValue(Value);

impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(StrictValueVisitor)
    }
}

struct StrictValueVisitor;

impl<'de> Visitor<'de> for StrictValueVisitor {
    type Value = StrictValue;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::Bool(value)))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::Number(value.into())))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::Number(value.into())))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(StrictValue(Value::Number(
            serde_json::Number::from_f64(value)
                .ok_or_else(|| de::Error::custom("non-finite number"))?,
        )))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::String(value.to_owned())))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::String(value)))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::Null))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(StrictValue(Value::Null))
    }

    fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        StrictValue::deserialize(deserializer)
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(value) = sequence.next_element::<StrictValue>()? {
            values.push(value.0);
        }
        Ok(StrictValue(Value::Array(values)))
    }

    fn visit_map<A>(self, mut source: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut object = Map::new();
        while let Some((key, value)) = source.next_entry::<String, StrictValue>()? {
            if object.contains_key(&key) {
                return Err(de::Error::custom(format!("duplicate object key: {key}")));
            }
            object.insert(key, value.0);
        }
        Ok(StrictValue(Value::Object(object)))
    }
}
