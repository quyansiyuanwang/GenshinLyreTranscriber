//! Versioned protocol types and semantic validation.

use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const SAFE_INTEGER_MAX: u64 = 9_007_199_254_740_991;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    code: &'static str,
    message: String,
}

impl ValidationError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ValidationError {}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NoteSequence {
    pub schema_version: u8,
    pub time_unit: String,
    pub duration_us: u64,
    pub notes: Vec<Note>,
    pub tempo_map: Vec<TempoPoint>,
    pub beat_grid: Vec<BeatGridPoint>,
    pub provenance: Provenance,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Note {
    pub pitch: u8,
    pub start_us: u64,
    pub end_us: u64,
    pub velocity: u8,
    pub confidence: Option<f64>,
    pub track: u32,
    pub channel: u8,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TempoPoint {
    pub at_us: u64,
    pub bpm: f64,
    pub source: TempoSource,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TempoSource {
    Midi,
    Estimated,
    Manual,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BeatGridPoint {
    pub at_us: u64,
    pub beat_position: f64,
    pub bpm: f64,
    pub confidence: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Provenance {
    pub source_type: SourceType,
    pub source_offset_us: u64,
    pub model_version: Option<String>,
    pub parameters: Map<String, Value>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SourceType {
    Audio,
    Video,
    Midi,
}

impl NoteSequence {
    pub fn validate(&self) -> Result<(), ValidationError> {
        if self.schema_version != 1 || self.time_unit != "us" {
            return Err(ValidationError::new(
                "UNSUPPORTED_VERSION",
                "NoteSequence must use schema version 1 with microsecond time",
            ));
        }
        require_safe_time(self.duration_us, "/duration_us")?;
        let mut previous: Option<(u64, u8, u32)> = None;
        for note in &self.notes {
            require_safe_time(note.start_us, "/notes/start_us")?;
            require_safe_time(note.end_us, "/notes/end_us")?;
            if note.start_us >= note.end_us || note.end_us > self.duration_us {
                return Err(ValidationError::new(
                    "NOTE_RANGE",
                    "note must satisfy start_us < end_us <= duration_us",
                ));
            }
            if note.pitch > 127 || note.velocity == 0 || note.velocity > 127 {
                return Err(ValidationError::new(
                    "NOTE_RANGE",
                    "pitch and velocity must be in MIDI range",
                ));
            }
            validate_optional_confidence(note.confidence)?;
            if note.channel > 15 {
                return Err(ValidationError::new(
                    "NOTE_RANGE",
                    "channel must be in range 0..15",
                ));
            }
            let order = (note.start_us, note.pitch, note.track);
            if previous.is_some_and(|value| order < value) {
                return Err(ValidationError::new("NOTE_ORDER", "notes are not sorted"));
            }
            previous = Some(order);
        }
        for point in &self.tempo_map {
            require_safe_time(point.at_us, "/tempo_map/at_us")?;
            validate_bpm(point.bpm)?;
        }
        if !self
            .tempo_map
            .windows(2)
            .all(|pair| pair[0].at_us <= pair[1].at_us)
        {
            return Err(ValidationError::new(
                "TEMPO_ORDER",
                "tempo_map is not ordered",
            ));
        }
        for point in &self.beat_grid {
            require_safe_time(point.at_us, "/beat_grid/at_us")?;
            validate_bpm(point.bpm)?;
            if !point.beat_position.is_finite() {
                return Err(ValidationError::new(
                    "BEAT_ORDER",
                    "beat position must be finite",
                ));
            }
            validate_optional_confidence(point.confidence)?;
        }
        if !self
            .beat_grid
            .windows(2)
            .all(|pair| pair[0].at_us <= pair[1].at_us)
        {
            return Err(ValidationError::new(
                "BEAT_ORDER",
                "beat_grid is not ordered",
            ));
        }
        require_safe_time(
            self.provenance.source_offset_us,
            "/provenance/source_offset_us",
        )
    }
}

pub fn validate_events_value(value: &Value) -> Result<(), ValidationError> {
    let object = value.as_object().ok_or_else(|| {
        ValidationError::new("SCHEMA_INVALID", "events document must be an object")
    })?;
    require_version(object, "format_version", 1)?;
    let duration_us = require_value_time(object.get("duration_us"), "/duration_us")?;
    let events = object
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "events must be an array"))?;
    let mut previous: Option<u64> = None;
    for event in events {
        let event = event
            .as_object()
            .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "event must be an object"))?;
        let at_us = require_value_time(event.get("at_us"), "/events/at_us")?;
        if at_us > duration_us {
            return Err(ValidationError::new(
                "DURATION_RANGE",
                "event is after duration_us",
            ));
        }
        if previous.is_some_and(|value| at_us <= value) {
            return Err(ValidationError::new(
                "EVENT_ORDER",
                "event times must be strictly increasing",
            ));
        }
        previous = Some(at_us);
    }
    Ok(())
}

pub fn validate_worker_value(value: &Value) -> Result<(), ValidationError> {
    let object = value.as_object().ok_or_else(|| {
        ValidationError::new("SCHEMA_INVALID", "worker message must be an object")
    })?;
    require_version(object, "protocol_version", 1)
}

pub fn validate_note_sequence_value(value: &Value) -> Result<(), ValidationError> {
    let object = value
        .as_object()
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "NoteSequence must be an object"))?;
    require_version(object, "schema_version", 1)?;
    let sequence: NoteSequence = serde_json::from_value(value.clone())
        .map_err(|error| ValidationError::new("SCHEMA_INVALID", error.to_string()))?;
    sequence.validate()
}

pub fn validate_report_value(value: &Value) -> Result<(), ValidationError> {
    let object = value
        .as_object()
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "report must be an object"))?;
    require_version(object, "schema_version", 1)?;
    let artifacts = object
        .get("artifacts")
        .and_then(Value::as_array)
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "artifacts must be an array"))?;
    for artifact in artifacts {
        let path = artifact
            .get("relative_path")
            .and_then(Value::as_str)
            .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", "relative_path is required"))?;
        if !is_safe_relative_path(path) {
            return Err(ValidationError::new(
                "UNSAFE_PATH",
                "artifact path must be relative and cannot escape the result directory",
            ));
        }
    }
    Ok(())
}

fn require_version(
    object: &Map<String, Value>,
    field: &str,
    expected: u64,
) -> Result<(), ValidationError> {
    let value = object
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", format!("{field} is required")))?;
    if value != expected {
        return Err(ValidationError::new(
            "UNSUPPORTED_VERSION",
            format!("unsupported {field}: {value}"),
        ));
    }
    Ok(())
}

fn require_value_time(value: Option<&Value>, pointer: &str) -> Result<u64, ValidationError> {
    let value = value
        .and_then(Value::as_u64)
        .ok_or_else(|| ValidationError::new("SCHEMA_INVALID", format!("{pointer} is invalid")))?;
    require_safe_time(value, pointer)?;
    Ok(value)
}

fn require_safe_time(value: u64, pointer: &str) -> Result<(), ValidationError> {
    if value > SAFE_INTEGER_MAX {
        return Err(ValidationError::new(
            "SCHEMA_INVALID",
            format!("{pointer} is outside the safe integer range"),
        ));
    }
    Ok(())
}

fn validate_bpm(bpm: f64) -> Result<(), ValidationError> {
    if !bpm.is_finite() || bpm <= 0.0 || bpm > 1000.0 {
        return Err(ValidationError::new("NOTE_RANGE", "BPM is invalid"));
    }
    Ok(())
}

fn validate_optional_confidence(confidence: Option<f64>) -> Result<(), ValidationError> {
    if confidence.is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value)) {
        return Err(ValidationError::new(
            "NOTE_RANGE",
            "confidence must be null or in range 0..1",
        ));
    }
    Ok(())
}

fn is_safe_relative_path(path: &str) -> bool {
    let normalized = path.replace('\\', "/");
    let is_windows_absolute = normalized.get(1..2).is_some_and(|value| value == ":");
    !normalized.starts_with('/')
        && !is_windows_absolute
        && normalized
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}
