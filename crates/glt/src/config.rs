//! Versioned portable configuration shared by CLI and TUI.

use std::collections::HashSet;
use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::cli::{
    ArrangementOptions, ArrangementProfile, CleaningOptions, CleaningProfile, CliError,
};
use crate::jobs::{MappingKey, StartOptions, Timing, Transpose};

pub const CONFIG_FORMAT_VERSION: u32 = 1;
pub const DEFAULT_MAPPING_PROFILE: &str = "lyre-21-default";
pub const KEYBOARD_ORDER: &str = "ZXCVBNMASDFGHJQWERTYU";
pub const NATURAL_PITCHES: [u8; 21] = [
    48, 50, 52, 53, 55, 57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77, 79, 81, 83,
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigDocument {
    pub format_version: u32,
    pub name: String,
    pub parameters: ConfigParameters,
    pub mapping: ConfigMapping,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigParameters {
    pub cleaning_profile: CleaningProfile,
    pub arrangement: ArrangementProfile,
    pub min_confidence: Option<f64>,
    pub min_duration_ms: Option<u64>,
    pub retrigger_gap_ms: Option<u64>,
    pub timing: Timing,
    pub bpm: Option<f64>,
    pub transpose: Transpose,
    pub onset_window_ms: u64,
    pub max_voices: u8,
    pub preview_wav: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigMapping {
    pub profile: String,
    pub keys: Vec<MappingKey>,
}

impl Default for ConfigDocument {
    fn default() -> Self {
        Self {
            format_version: CONFIG_FORMAT_VERSION,
            name: "默认配置".to_owned(),
            parameters: ConfigParameters {
                cleaning_profile: CleaningProfile::Auto,
                arrangement: ArrangementProfile::Balanced,
                min_confidence: None,
                min_duration_ms: None,
                retrigger_gap_ms: None,
                timing: Timing::Auto,
                bpm: None,
                transpose: Transpose::automatic(),
                onset_window_ms: 150,
                max_voices: 2,
                preview_wav: true,
            },
            mapping: ConfigMapping {
                profile: DEFAULT_MAPPING_PROFILE.to_owned(),
                keys: default_mapping_keys(),
            },
        }
    }
}

impl ConfigDocument {
    pub fn load(path: &Path) -> Result<Self, CliError> {
        if !path.is_file() {
            return Err(CliError::InvalidArgument(format!(
                "config file does not exist: {}",
                path.display()
            )));
        }
        let text = fs::read_to_string(path).map_err(|error| {
            CliError::InvalidArgument(format!("cannot read config {}: {error}", path.display()))
        })?;
        let document: Self = serde_json::from_str(&text).map_err(|error| {
            CliError::InvalidArgument(format!("invalid config {}: {error}", path.display()))
        })?;
        document.validate()?;
        Ok(document)
    }

    pub fn save(&self, path: &Path, overwrite: bool) -> Result<(), CliError> {
        self.validate()?;
        if path.exists() && !overwrite {
            return Err(CliError::InvalidArgument(format!(
                "config file already exists: {}",
                path.display()
            )));
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                CliError::InvalidArgument(format!("cannot create {}: {error}", parent.display()))
            })?;
        }
        let text = serde_json::to_string_pretty(self).map_err(|error| {
            CliError::InvalidArgument(format!("cannot serialize config: {error}"))
        })?;
        fs::write(path, format!("{text}\n")).map_err(|error| {
            CliError::InvalidArgument(format!("cannot write config {}: {error}", path.display()))
        })
    }

    pub fn validate(&self) -> Result<(), CliError> {
        if self.format_version != CONFIG_FORMAT_VERSION {
            return Err(CliError::InvalidArgument(format!(
                "unsupported config version: {}",
                self.format_version
            )));
        }
        if self.name.trim().is_empty() || self.name.chars().count() > 128 {
            return Err(CliError::InvalidArgument(
                "config name must contain 1..=128 characters".to_owned(),
            ));
        }
        self.parameters.validate()?;
        validate_mapping(&self.mapping)
    }

    pub fn from_options(
        name: String,
        options: &StartOptions,
        cleaning: &CleaningOptions,
        arrangement: &ArrangementOptions,
    ) -> Result<Self, CliError> {
        let document = Self {
            format_version: CONFIG_FORMAT_VERSION,
            name,
            parameters: ConfigParameters {
                cleaning_profile: cleaning.profile,
                arrangement: arrangement.profile,
                min_confidence: cleaning.min_confidence,
                min_duration_ms: cleaning.min_duration_us.map(|value| value / 1000),
                retrigger_gap_ms: cleaning.retrigger_gap_us.map(|value| value / 1000),
                timing: options.timing.unwrap_or(Timing::Auto),
                bpm: options.bpm,
                transpose: options
                    .transpose
                    .clone()
                    .unwrap_or_else(Transpose::automatic),
                onset_window_ms: arrangement.onset_window_us / 1000,
                max_voices: arrangement.max_voices,
                preview_wav: options.preview_wav.unwrap_or(true),
            },
            mapping: ConfigMapping {
                profile: options
                    .mapping_profile
                    .clone()
                    .unwrap_or_else(|| DEFAULT_MAPPING_PROFILE.to_owned()),
                keys: options
                    .mapping_keys
                    .clone()
                    .unwrap_or_else(default_mapping_keys),
            },
        };
        document.validate()?;
        Ok(document)
    }

    pub fn apply_to_options(&self, options: &mut StartOptions) {
        options.mapping_profile = Some(self.mapping.profile.clone());
        options.mapping_keys = Some(self.mapping.keys.clone());
    }
}

impl ConfigParameters {
    pub fn validate(&self) -> Result<(), CliError> {
        if let Some(value) = self.min_confidence
            && (!value.is_finite() || !(0.0..=1.0).contains(&value))
        {
            return Err(CliError::InvalidArgument(
                "min_confidence must be in range 0..=1".to_owned(),
            ));
        }
        if let Some(value) = self.bpm
            && (!value.is_finite() || value <= 0.0 || value > 1000.0)
        {
            return Err(CliError::InvalidArgument(
                "bpm must be in range (0, 1000]".to_owned(),
            ));
        }
        match &self.transpose {
            Transpose::Auto(value) if value != "auto" => {
                return Err(CliError::InvalidArgument(
                    "transpose string must be 'auto'".to_owned(),
                ));
            }
            Transpose::Semitones(value) if !(-48..=48).contains(value) => {
                return Err(CliError::InvalidArgument(
                    "transpose must be in range -48..=48".to_owned(),
                ));
            }
            _ => {}
        }
        if !(1..=1_000).contains(&self.onset_window_ms) {
            return Err(CliError::InvalidArgument(
                "onset_window_ms must be in range 1..=1000".to_owned(),
            ));
        }
        if !(1..=21).contains(&self.max_voices) {
            return Err(CliError::InvalidArgument(
                "max_voices must be in range 1..=21".to_owned(),
            ));
        }
        Ok(())
    }
}

pub fn default_mapping_keys() -> Vec<MappingKey> {
    KEYBOARD_ORDER
        .chars()
        .zip(NATURAL_PITCHES)
        .map(|(key, pitch)| MappingKey {
            key: key.to_string(),
            pitch,
        })
        .collect()
}

pub fn validate_mapping(mapping: &ConfigMapping) -> Result<(), CliError> {
    if mapping.profile.trim().is_empty() || mapping.profile.chars().count() > 128 {
        return Err(CliError::InvalidArgument(
            "mapping profile must contain 1..=128 characters".to_owned(),
        ));
    }
    if mapping.keys.len() != 21 {
        return Err(CliError::InvalidArgument(
            "mapping keys must contain exactly 21 entries".to_owned(),
        ));
    }
    let allowed: HashSet<char> = KEYBOARD_ORDER.chars().collect();
    let mut keys = HashSet::new();
    let mut pitches = HashSet::new();
    for entry in &mapping.keys {
        let mut chars = entry.key.chars();
        if entry.key.chars().count() != 1 || !chars.next().is_some_and(|key| allowed.contains(&key))
        {
            return Err(CliError::InvalidArgument(format!(
                "invalid lyre key: {}",
                entry.key
            )));
        }
        if !NATURAL_PITCHES.contains(&entry.pitch)
            || !matches!(entry.pitch % 12, 0 | 2 | 4 | 5 | 7 | 9 | 11)
        {
            return Err(CliError::InvalidArgument(format!(
                "mapping pitch is outside C3-B5 naturals: {}",
                entry.pitch
            )));
        }
        if !keys.insert(entry.key.clone()) {
            return Err(CliError::InvalidArgument(
                "mapping keys must be unique".to_owned(),
            ));
        }
        if !pitches.insert(entry.pitch) {
            return Err(CliError::InvalidArgument(
                "mapping pitches must be unique".to_owned(),
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_round_trips() {
        let document = ConfigDocument::default();
        document.validate().unwrap();
        let text = serde_json::to_string(&document).unwrap();
        let parsed: ConfigDocument = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed, document);
    }

    #[test]
    fn rejects_duplicate_mapping_pitch() {
        let mut document = ConfigDocument::default();
        document.mapping.keys[1].pitch = document.mapping.keys[0].pitch;
        assert!(document.validate().is_err());
    }

    #[test]
    fn rejects_unknown_version() {
        let document = ConfigDocument {
            format_version: 2,
            ..ConfigDocument::default()
        };
        assert!(document.validate().is_err());
    }

    #[test]
    fn save_does_not_replace_existing_config_without_explicit_overwrite() {
        let root = std::env::temp_dir().join(format!("glt-config-save-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("config.json");
        fs::write(&path, b"existing").unwrap();
        let document = ConfigDocument::default();
        assert!(document.save(&path, false).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"existing");
        document.save(&path, true).unwrap();
        assert_eq!(ConfigDocument::load(&path).unwrap(), document);
        let _ = fs::remove_dir_all(root);
    }
}
