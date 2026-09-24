//! Minimal terminal UI for choosing inputs, parameters and running one job.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crossterm::cursor::Show;
use crossterm::event::{
    self, DisableBracketedPaste, EnableBracketedPaste, Event, KeyCode, KeyEvent, KeyEventKind,
    KeyModifiers,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Block, Borders, Gauge, Paragraph, Wrap};
use ratatui::{Frame, Terminal};

use crate::cli::{
    ArrangementOptions, ArrangementProfile, CleaningOptions, CleaningProfile, CliError, JobOutcome,
    JobUpdate, build_arrangement_options, build_cleaning_options, build_job_options,
    run_job_with_cancel,
};
use crate::jobs::{
    FilterRange, FilterRule, FilterSpec, IntegerFilterRange, Operation, StartOptions, Timing,
    Transpose,
};
use crate::preview::PlaybackService;

const FIELD_INPUT: usize = 0;
const FIELD_OUTPUT: usize = 1;
const FIELD_TIMING: usize = 2;
const FIELD_BPM: usize = 3;
const FIELD_TRANSPOSE: usize = 4;
const FIELD_TRACK: usize = 5;
const FIELD_START: usize = 6;
const FIELD_END: usize = 7;
const FIELD_PREVIEW: usize = 8;
const FIELD_OVERWRITE: usize = 9;
const FIELD_CLEANING_PROFILE: usize = 10;
const FIELD_MIN_CONFIDENCE: usize = 11;
const FIELD_MIN_DURATION: usize = 12;
const FIELD_RETRIGGER_GAP: usize = 13;
const FIELD_ARRANGEMENT: usize = 14;
const FIELD_ONSET_WINDOW: usize = 15;
const FIELD_MAX_VOICES: usize = 16;
const FIELD_COUNT: usize = 17;
const FILTER_RULE_COUNT: usize = 4;
const FILTER_FIELD_COUNT: usize = 9;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Screen {
    Input,
    Parameters,
    Filter,
    Running,
    Completed,
    Failed,
}

#[derive(Debug, Clone, Default)]
struct FilterRuleForm {
    enabled: bool,
    confidence_min: String,
    confidence_max: String,
    duration_min: String,
    duration_max: String,
    velocity_min: String,
    velocity_max: String,
    pitch_min: String,
    pitch_max: String,
}

impl FilterRuleForm {
    fn from_rule(rule: &FilterRule) -> Self {
        Self {
            enabled: rule.enabled,
            confidence_min: rule
                .confidence
                .as_ref()
                .map(|range| format_number(range.min))
                .unwrap_or_default(),
            confidence_max: rule
                .confidence
                .as_ref()
                .map(|range| format_number(range.max))
                .unwrap_or_default(),
            duration_min: rule
                .duration_ms
                .as_ref()
                .map(|range| format_number(range.min))
                .unwrap_or_default(),
            duration_max: rule
                .duration_ms
                .as_ref()
                .map(|range| format_number(range.max))
                .unwrap_or_default(),
            velocity_min: rule
                .velocity
                .as_ref()
                .map(|range| range.min.to_string())
                .unwrap_or_default(),
            velocity_max: rule
                .velocity
                .as_ref()
                .map(|range| range.max.to_string())
                .unwrap_or_default(),
            pitch_min: rule
                .pitch
                .as_ref()
                .map(|range| format_pitch(range.min))
                .unwrap_or_default(),
            pitch_max: rule
                .pitch
                .as_ref()
                .map(|range| format_pitch(range.max))
                .unwrap_or_default(),
        }
    }

    fn to_rule(&self) -> Result<FilterRule, CliError> {
        Ok(FilterRule {
            enabled: self.enabled,
            confidence: optional_range(
                &self.confidence_min,
                &self.confidence_max,
                0.0,
                1.0,
                "confidence",
            )?,
            duration_ms: optional_range(
                &self.duration_min,
                &self.duration_max,
                0.0,
                3_600_000.0,
                "duration-ms",
            )?,
            velocity: optional_integer_range(
                &self.velocity_min,
                &self.velocity_max,
                1,
                127,
                "velocity",
            )?,
            pitch: optional_pitch_range(&self.pitch_min, &self.pitch_max)?,
        })
    }

    fn field_mut(&mut self, index: usize) -> Option<&mut String> {
        match index {
            1 => Some(&mut self.confidence_min),
            2 => Some(&mut self.confidence_max),
            3 => Some(&mut self.duration_min),
            4 => Some(&mut self.duration_max),
            5 => Some(&mut self.velocity_min),
            6 => Some(&mut self.velocity_max),
            7 => Some(&mut self.pitch_min),
            8 => Some(&mut self.pitch_max),
            _ => None,
        }
    }
}

impl From<&FilterSpec> for [FilterRuleForm; FILTER_RULE_COUNT] {
    fn from(spec: &FilterSpec) -> Self {
        let mut forms: [FilterRuleForm; FILTER_RULE_COUNT] =
            std::array::from_fn(|_| Default::default());
        for (index, rule) in spec.rules.iter().take(FILTER_RULE_COUNT).enumerate() {
            forms[index] = FilterRuleForm::from_rule(rule);
        }
        forms
    }
}

#[derive(Debug)]
enum UiJobEvent {
    Update(JobUpdate),
    Completed(JobOutcome),
    Cancelled,
    Failed(String),
}

struct Browser {
    directory: PathBuf,
    entries: Vec<PathBuf>,
    selected: usize,
}

impl Browser {
    fn open(path: &Path) -> Self {
        let directory = if path.is_dir() {
            path.to_path_buf()
        } else {
            path.parent()
                .unwrap_or_else(|| Path::new("."))
                .to_path_buf()
        };
        let mut browser = Self {
            directory,
            entries: Vec::new(),
            selected: 0,
        };
        browser.refresh();
        browser
    }

    fn refresh(&mut self) {
        self.entries.clear();
        if let Ok(entries) = std::fs::read_dir(&self.directory) {
            let mut paths: Vec<PathBuf> = entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .collect();
            paths.sort_by(|left, right| {
                right
                    .is_dir()
                    .cmp(&left.is_dir())
                    .then_with(|| left.file_name().cmp(&right.file_name()))
            });
            if let Some(parent) = self.directory.parent() {
                paths.insert(0, parent.to_path_buf());
            }
            self.entries = paths;
        }
        self.entries.truncate(500);
        self.selected = self.selected.min(self.entries.len().saturating_sub(1));
    }

    fn selected_path(&self) -> Option<PathBuf> {
        self.entries.get(self.selected).cloned()
    }
}

pub struct TuiApp {
    screen: Screen,
    fields: [String; FIELD_COUNT],
    focus: usize,
    browser: Option<Browser>,
    warnings: Vec<String>,
    stage: String,
    fraction: Option<f64>,
    message: String,
    result_directory: Option<PathBuf>,
    result_lines: Vec<String>,
    result_scroll: u16,
    filter_rules: [FilterRuleForm; FILTER_RULE_COUNT],
    filter_rule_index: usize,
    filter_field_index: usize,
    filter_seed: FilterSpec,
    filter_transpose: i32,
    filter_cache_available: bool,
    preview_path: Option<PathBuf>,
    playback: Option<PlaybackService>,
    playback_paused: bool,
    playback_volume: f32,
    receiver: Option<Receiver<UiJobEvent>>,
    cancel: Option<Arc<AtomicBool>>,
    join_handle: Option<JoinHandle<()>>,
}

impl Default for TuiApp {
    fn default() -> Self {
        let fields = std::array::from_fn(|index| match index {
            FIELD_TIMING => "auto".to_owned(),
            FIELD_TRANSPOSE => "auto".to_owned(),
            FIELD_PREVIEW => "true".to_owned(),
            FIELD_OVERWRITE => "false".to_owned(),
            FIELD_CLEANING_PROFILE => "auto".to_owned(),
            FIELD_MIN_CONFIDENCE | FIELD_MIN_DURATION | FIELD_RETRIGGER_GAP => String::new(),
            FIELD_ARRANGEMENT => "balanced".to_owned(),
            FIELD_ONSET_WINDOW => "150".to_owned(),
            FIELD_MAX_VOICES => "2".to_owned(),
            _ => String::new(),
        });
        Self {
            screen: Screen::Input,
            fields,
            focus: FIELD_INPUT,
            browser: None,
            warnings: Vec::new(),
            stage: String::new(),
            fraction: None,
            message: String::new(),
            result_directory: None,
            result_lines: Vec::new(),
            result_scroll: 0,
            filter_rules: std::array::from_fn(|_| Default::default()),
            filter_rule_index: 0,
            filter_field_index: 1,
            filter_seed: FilterSpec::default(),
            filter_transpose: 0,
            filter_cache_available: false,
            preview_path: None,
            playback: None,
            playback_paused: true,
            playback_volume: 1.0,
            receiver: None,
            cancel: None,
            join_handle: None,
        }
    }
}

impl TuiApp {
    fn field(&self, index: usize) -> &str {
        &self.fields[index]
    }

    #[cfg(test)]
    fn set_field(&mut self, index: usize, value: String) {
        self.fields[index] = value;
    }

    fn operation(&self) -> Operation {
        match Path::new(self.field(FIELD_INPUT))
            .extension()
            .and_then(|value| value.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("mid" | "midi") => Operation::ConvertMidi,
            _ => Operation::Transcribe,
        }
    }

    fn build_options(&self) -> Result<StartOptions, CliError> {
        let operation = self.operation();
        let timing = parse_timing(self.field(FIELD_TIMING))?;
        let transpose = parse_transpose(self.field(FIELD_TRANSPOSE))?;
        let bpm = if operation == Operation::Transcribe {
            parse_optional_f64(self.field(FIELD_BPM), "bpm")?
        } else {
            None
        };
        let audio_track = if operation == Operation::Transcribe {
            parse_optional_u32(self.field(FIELD_TRACK), "audio-track")?
        } else {
            None
        };
        let start = if operation == Operation::Transcribe {
            parse_optional_f64(self.field(FIELD_START), "start-seconds")?
        } else {
            None
        };
        let end = if operation == Operation::Transcribe {
            parse_optional_f64(self.field(FIELD_END), "end-seconds")?
        } else {
            None
        };
        build_job_options(
            timing,
            bpm,
            transpose,
            audio_track,
            start,
            end,
            parse_bool(self.field(FIELD_PREVIEW))?,
            parse_bool(self.field(FIELD_OVERWRITE))?,
        )
    }

    fn build_clean_options(&self) -> Result<CleaningOptions, CliError> {
        build_cleaning_options(
            parse_cleaning_profile(self.field(FIELD_CLEANING_PROFILE))?,
            parse_optional_f64(self.field(FIELD_MIN_CONFIDENCE), "min-confidence")?,
            parse_optional_u64(self.field(FIELD_MIN_DURATION), "min-duration-ms")?,
            parse_optional_u64(self.field(FIELD_RETRIGGER_GAP), "retrigger-gap-ms")?,
        )
    }

    fn build_arrangement_options(&self) -> Result<ArrangementOptions, CliError> {
        build_arrangement_options(
            parse_arrangement_profile(self.field(FIELD_ARRANGEMENT))?,
            parse_required_u64(self.field(FIELD_ONSET_WINDOW), "onset-window-ms")?,
            parse_required_u8(self.field(FIELD_MAX_VOICES), "max-voices")?,
        )
    }

    fn start_job(&mut self) {
        let input = PathBuf::from(self.field(FIELD_INPUT));
        let output = PathBuf::from(self.field(FIELD_OUTPUT));
        if input.as_os_str().is_empty() || output.as_os_str().is_empty() {
            self.message = "input and output paths are required".to_owned();
            return;
        }
        let options = match self.build_options() {
            Ok(options) => options,
            Err(error) => {
                self.message = error.to_string();
                return;
            }
        };
        let cleaning = match self.build_clean_options() {
            Ok(options) => options,
            Err(error) => {
                self.message = error.to_string();
                return;
            }
        };
        let arrangement = match self.build_arrangement_options() {
            Ok(options) => options,
            Err(error) => {
                self.message = error.to_string();
                return;
            }
        };
        let operation = self.operation();
        self.launch_job(input, output, operation, options, cleaning, arrangement);
    }

    fn launch_job(
        &mut self,
        input: PathBuf,
        output: PathBuf,
        operation: Operation,
        options: StartOptions,
        cleaning: CleaningOptions,
        arrangement: ArrangementOptions,
    ) {
        let cancel = Arc::new(AtomicBool::new(false));
        let thread_cancel = Arc::clone(&cancel);
        let (sender, receiver) = mpsc::channel();
        self.receiver = Some(receiver);
        self.cancel = Some(cancel);
        self.warnings.clear();
        self.stage = "validating".to_owned();
        self.fraction = Some(0.0);
        self.result_directory = None;
        self.screen = Screen::Running;
        self.join_handle = Some(thread::spawn(move || {
            let result = run_job_with_cancel(
                None,
                input,
                output,
                operation,
                options,
                cleaning,
                arrangement,
                thread_cancel,
                |update| {
                    let _ = sender.send(UiJobEvent::Update(update));
                },
            );
            let event = match result {
                Ok(outcome) => UiJobEvent::Completed(outcome),
                Err(CliError::Cancelled) => UiJobEvent::Cancelled,
                Err(error) => UiJobEvent::Failed(error.to_string()),
            };
            let _ = sender.send(event);
        }));
    }

    fn open_filter(&mut self) {
        if !self.filter_cache_available {
            self.message =
                "this result has no candidate cache; rerun with the current worker".to_owned();
            return;
        }
        self.filter_rules = (&self.filter_seed).into();
        self.filter_rule_index = 0;
        self.filter_field_index = 1;
        self.screen = Screen::Filter;
        self.message = "F5 apply  R reset  Up/Down rule  Tab field".to_owned();
    }

    fn current_filter_spec(&self) -> Result<FilterSpec, CliError> {
        let mut rules = Vec::new();
        for form in &self.filter_rules {
            let has_range = !form.confidence_min.is_empty()
                || !form.confidence_max.is_empty()
                || !form.duration_min.is_empty()
                || !form.duration_max.is_empty()
                || !form.velocity_min.is_empty()
                || !form.velocity_max.is_empty()
                || !form.pitch_min.is_empty()
                || !form.pitch_max.is_empty();
            if form.enabled || has_range {
                rules.push(form.to_rule()?);
            }
        }
        let spec = FilterSpec {
            format_version: 1,
            rules,
        };
        spec.validate().map_err(CliError::InvalidArgument)?;
        Ok(spec)
    }

    fn start_filter_job(&mut self) {
        let Some(input) = self.result_directory.clone() else {
            self.message = "result directory is unavailable".to_owned();
            return;
        };
        let spec = match self.current_filter_spec() {
            Ok(spec) => spec,
            Err(error) => {
                self.message = error.to_string();
                return;
            }
        };
        let output = match next_filter_directory(&input) {
            Ok(output) => output,
            Err(error) => {
                self.message = error.to_string();
                return;
            }
        };
        let options = StartOptions {
            filter: Some(spec),
            preview_wav: Some(true),
            overwrite: Some(false),
            ..StartOptions::default()
        };
        self.launch_job(
            input,
            output,
            Operation::Refilter,
            options,
            CleaningOptions::default(),
            ArrangementOptions::default(),
        );
    }

    fn load_result(&mut self, outcome: &JobOutcome) {
        self.result_directory = Some(outcome.result.output_dir.clone());
        self.result_lines.clear();
        self.preview_path = None;
        self.playback = None;
        self.playback_paused = true;
        let report_path = outcome.result.output_dir.join(&outcome.result.report_path);
        let report: serde_json::Value = match std::fs::read_to_string(&report_path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
        {
            Some(report) => report,
            None => {
                self.result_lines
                    .push(format!("cannot read report: {}", report_path.display()));
                return;
            }
        };
        self.filter_seed = report
            .get("parameters")
            .and_then(|parameters| parameters.get("filter"))
            .and_then(|filter| serde_json::from_value::<FilterSpec>(filter.clone()).ok())
            .filter(|filter| filter.validate().is_ok())
            .unwrap_or_default();
        self.filter_rules = (&self.filter_seed).into();
        self.filter_rule_index = 0;
        self.filter_field_index = 1;
        let candidate_path = outcome.result.output_dir.join("score.candidates.json");
        self.filter_cache_available = candidate_path.is_file();
        self.filter_transpose = std::fs::read_to_string(&candidate_path)
            .ok()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
            .and_then(|cache| {
                cache
                    .get("resolved_transpose")
                    .and_then(serde_json::Value::as_i64)
            })
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0);
        if let Some(counts) = report.get("counts").and_then(serde_json::Value::as_object) {
            for key in [
                "input_notes",
                "output_notes",
                "dropped_notes",
                "mapped_keys",
                "replaced_semitones",
                "octave_folds",
                "duplicate_keys",
                "compatibility_collisions",
            ] {
                if let Some(value) = counts.get(key) {
                    self.result_lines.push(format!("{key}: {value}"));
                }
            }
        }
        if let Some(warnings) = report.get("warnings").and_then(serde_json::Value::as_array) {
            for warning in warnings {
                let code = warning
                    .get("code")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("WARNING");
                let message = warning
                    .get("message")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("");
                self.result_lines
                    .push(format!("warning [{code}] {message}"));
            }
        }
        if let Some(artifacts) = report
            .get("artifacts")
            .and_then(serde_json::Value::as_array)
        {
            self.result_lines.push("artifacts:".to_owned());
            for artifact in artifacts {
                let kind = artifact
                    .get("kind")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("?");
                let relative = artifact
                    .get("relative_path")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("?");
                self.result_lines.push(format!("  {kind}: {relative}"));
                if kind == "preview_wav" {
                    self.preview_path = Some(outcome.result.output_dir.join(relative));
                }
            }
        }
    }

    fn toggle_playback(&mut self) {
        let Some(path) = self.preview_path.clone() else {
            self.message = "no preview.wav; rerun with preview_wav enabled".to_owned();
            return;
        };
        if self.playback.is_none() {
            self.playback = Some(PlaybackService::open_wav(path));
        }
        let Some(playback) = self.playback.as_mut() else {
            return;
        };
        if let Some(error) = playback.error() {
            self.message = error.to_string();
            return;
        }
        let result = if self.playback_paused {
            playback.play()
        } else {
            playback.pause()
        };
        match result {
            Ok(()) => {
                self.playback_paused = !self.playback_paused;
                self.message = if self.playback_paused {
                    "preview paused".to_owned()
                } else {
                    "preview playing".to_owned()
                };
            }
            Err(error) => self.message = error.to_string(),
        }
    }

    fn stop_playback(&mut self) {
        if let Some(playback) = self.playback.as_mut()
            && let Err(error) = playback.stop()
        {
            self.message = error.to_string();
        }
        self.playback_paused = true;
    }

    fn adjust_volume(&mut self, delta: f32) {
        self.playback_volume = (self.playback_volume + delta).clamp(0.0, 1.0);
        if let Some(playback) = self.playback.as_mut()
            && let Err(error) = playback.set_volume(self.playback_volume)
        {
            self.message = error.to_string();
        }
    }

    fn handle_filter_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                self.screen = Screen::Completed;
                self.message.clear();
            }
            KeyCode::F(5) => self.start_filter_job(),
            KeyCode::Up => {
                self.filter_rule_index = self.filter_rule_index.saturating_sub(1);
            }
            KeyCode::Down => {
                self.filter_rule_index = (self.filter_rule_index + 1).min(FILTER_RULE_COUNT - 1);
            }
            KeyCode::Left | KeyCode::BackTab => {
                self.filter_field_index =
                    (self.filter_field_index + FILTER_FIELD_COUNT - 1) % FILTER_FIELD_COUNT;
            }
            KeyCode::Right | KeyCode::Tab => {
                self.filter_field_index = (self.filter_field_index + 1) % FILTER_FIELD_COUNT;
            }
            KeyCode::Char('r') => {
                self.filter_rules = (&self.filter_seed).into();
                self.message = "filter reset to source result".to_owned();
            }
            KeyCode::Char(' ') if self.filter_field_index == 0 => {
                let rule = &mut self.filter_rules[self.filter_rule_index];
                rule.enabled = !rule.enabled;
            }
            KeyCode::Delete => {
                if let Some(field) =
                    self.filter_rules[self.filter_rule_index].field_mut(self.filter_field_index)
                {
                    field.clear();
                }
            }
            KeyCode::Backspace => {
                if let Some(field) =
                    self.filter_rules[self.filter_rule_index].field_mut(self.filter_field_index)
                {
                    field.pop();
                }
            }
            KeyCode::Char(character)
                if self.filter_field_index > 0
                    && !key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                if let Some(field) =
                    self.filter_rules[self.filter_rule_index].field_mut(self.filter_field_index)
                {
                    field.push(character);
                }
            }
            _ => {}
        }
    }

    fn poll_job(&mut self) {
        let mut events = Vec::new();
        if let Some(receiver) = self.receiver.as_ref() {
            while let Ok(event) = receiver.try_recv() {
                events.push(event);
            }
        }
        for event in events {
            match event {
                UiJobEvent::Update(JobUpdate::Progress { stage, fraction }) => {
                    self.stage = stage;
                    self.fraction = fraction;
                }
                UiJobEvent::Update(JobUpdate::Warning { code, message }) => {
                    self.warnings.push(format!("[{code}] {message}"));
                }
                UiJobEvent::Completed(outcome) => {
                    self.load_result(&outcome);
                    self.message = format!("job {} completed", outcome.job_id);
                    self.screen = Screen::Completed;
                    self.cancel = None;
                    self.receiver = None;
                }
                UiJobEvent::Cancelled => {
                    self.message = "job cancelled".to_owned();
                    self.screen = Screen::Failed;
                    self.cancel = None;
                    self.receiver = None;
                }
                UiJobEvent::Failed(error) => {
                    self.message = error;
                    self.screen = Screen::Failed;
                    self.cancel = None;
                    self.receiver = None;
                }
            }
        }
    }

    fn request_cancel(&mut self) {
        if let Some(cancel) = &self.cancel {
            cancel.store(true, Ordering::SeqCst);
        }
        if let Some(handle) = self.join_handle.take() {
            let _ = handle.join();
        }
    }

    fn handle_paste(&mut self, text: &str) {
        if self.screen == Screen::Running || self.screen == Screen::Filter || self.browser.is_some()
        {
            return;
        }
        let Some(mut path) = text
            .lines()
            .map(str::trim)
            .find(|line| !line.is_empty())
            .map(|line| line.trim_matches(['"', '\'']).to_owned())
        else {
            return;
        };
        if let Some(uri_path) = path.strip_prefix("file:///") {
            path = uri_path.replace('/', "\\");
            if let Some(first) = path.get_mut(0..1) {
                first.make_ascii_uppercase();
            }
        }
        if self.focus == FIELD_OUTPUT {
            self.fields[FIELD_OUTPUT] = path;
            return;
        }
        if self.fields[FIELD_OUTPUT].trim().is_empty() {
            let source = PathBuf::from(&path);
            if let (Some(parent), Some(stem)) = (source.parent(), source.file_stem()) {
                self.fields[FIELD_OUTPUT] = parent
                    .join(format!("{}-output", stem.to_string_lossy()))
                    .display()
                    .to_string();
            }
        }
        if Path::new(&path)
            .extension()
            .and_then(|value| value.to_str())
            .is_some_and(|value| {
                value.eq_ignore_ascii_case("mid") || value.eq_ignore_ascii_case("midi")
            })
        {
            self.fields[FIELD_TIMING] = "preserve".to_owned();
        }
        self.fields[FIELD_INPUT] = path;
        self.focus = FIELD_INPUT;
    }

    fn handle_key(&mut self, key: KeyEvent) -> bool {
        if !matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) {
            return false;
        }
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.request_cancel();
            return true;
        }
        if self.browser.is_some() {
            self.handle_browser_key(key);
            return false;
        }
        if self.screen == Screen::Running {
            return false;
        }
        if self.screen == Screen::Filter {
            self.handle_filter_key(key);
            return false;
        }
        if self.screen == Screen::Completed {
            match key.code {
                KeyCode::Char(' ') => self.toggle_playback(),
                KeyCode::Char('s') => self.stop_playback(),
                KeyCode::Char('+' | '=') => self.adjust_volume(0.1),
                KeyCode::Char('-') => self.adjust_volume(-0.1),
                KeyCode::Char('f') => self.open_filter(),
                KeyCode::Char('r') => {
                    self.screen = Screen::Parameters;
                    self.focus = FIELD_TIMING;
                }
                KeyCode::Up => self.result_scroll = self.result_scroll.saturating_sub(1),
                KeyCode::Down => self.result_scroll = self.result_scroll.saturating_add(1),
                _ => {}
            }
            return false;
        }
        if self.screen == Screen::Failed && key.code == KeyCode::Char('r') {
            self.screen = Screen::Parameters;
            self.focus = FIELD_TIMING;
            return false;
        }
        if key.code == KeyCode::Esc {
            return true;
        }
        match key.code {
            KeyCode::F(2) => {
                let seed = PathBuf::from(self.field(FIELD_INPUT));
                self.browser = Some(Browser::open(&seed));
            }
            KeyCode::F(5) => self.start_job(),
            KeyCode::Enter => {
                if self.screen == Screen::Input {
                    if self.field(FIELD_OUTPUT).trim().is_empty() {
                        self.focus = FIELD_OUTPUT;
                    } else {
                        self.screen = Screen::Parameters;
                        self.focus = FIELD_TIMING;
                    }
                } else if self.focus + 1 < FIELD_COUNT {
                    self.focus += 1;
                } else {
                    self.start_job();
                }
            }
            KeyCode::Tab => {
                self.focus = (self.focus + 1) % FIELD_COUNT;
                self.screen = if self.focus <= FIELD_OUTPUT {
                    Screen::Input
                } else {
                    Screen::Parameters
                };
            }
            KeyCode::Backspace => {
                if self.focus <= FIELD_OUTPUT
                    || self.focus == FIELD_BPM
                    || self.focus == FIELD_TRANSPOSE
                    || self.focus == FIELD_TRACK
                    || self.focus == FIELD_START
                    || self.focus == FIELD_END
                    || self.focus == FIELD_CLEANING_PROFILE
                    || self.focus == FIELD_MIN_CONFIDENCE
                    || self.focus == FIELD_MIN_DURATION
                    || self.focus == FIELD_RETRIGGER_GAP
                    || self.focus == FIELD_ARRANGEMENT
                    || self.focus == FIELD_ONSET_WINDOW
                    || self.focus == FIELD_MAX_VOICES
                {
                    self.fields[self.focus].pop();
                }
            }
            KeyCode::Char(character) => {
                if self.focus == FIELD_TIMING {
                    self.fields[self.focus] =
                        next_timing(self.field(self.focus), character).to_owned();
                } else if self.focus == FIELD_CLEANING_PROFILE && character == ' ' {
                    self.fields[self.focus] =
                        next_cleaning_profile(self.field(self.focus)).to_owned();
                } else if self.focus == FIELD_ARRANGEMENT && character == ' ' {
                    self.fields[self.focus] =
                        next_arrangement_profile(self.field(self.focus)).to_owned();
                } else if self.focus == FIELD_PREVIEW || self.focus == FIELD_OVERWRITE {
                    if character == ' ' {
                        let value = !parse_bool(self.field(self.focus)).unwrap_or(false);
                        self.fields[self.focus] = value.to_string();
                    }
                } else if self.focus == FIELD_TRANSPOSE && character == ' ' {
                    self.fields[self.focus] = next_transpose(self.field(self.focus));
                } else {
                    self.fields[self.focus].push(character);
                }
            }
            _ => {}
        }
        false
    }

    fn handle_browser_key(&mut self, key: KeyEvent) {
        let Some(browser) = self.browser.as_mut() else {
            return;
        };
        match key.code {
            KeyCode::Esc => self.browser = None,
            KeyCode::Up => browser.selected = browser.selected.saturating_sub(1),
            KeyCode::Down => {
                browser.selected =
                    (browser.selected + 1).min(browser.entries.len().saturating_sub(1));
            }
            KeyCode::Enter => {
                if let Some(path) = browser.selected_path() {
                    if path.is_dir() {
                        browser.directory = path;
                        browser.selected = 0;
                        browser.refresh();
                    } else {
                        self.fields[self.focus] = path.display().to_string();
                        self.browser = None;
                    }
                }
            }
            _ => {}
        }
    }

    fn render(&self, frame: &mut Frame) {
        let area = frame.area();
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(2),
                Constraint::Min(3),
                Constraint::Length(2),
            ])
            .split(area);
        frame.render_widget(
            Paragraph::new(Line::from("GenshinLyreTranscriber")).style(
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ),
            chunks[0],
        );
        if let Some(browser) = &self.browser {
            let entries = if browser.entries.is_empty() {
                "(empty directory)".to_owned()
            } else {
                browser
                    .entries
                    .iter()
                    .enumerate()
                    .map(|(index, path)| {
                        let marker = if index == browser.selected { ">" } else { " " };
                        format!("{marker} {}", path.display())
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
            };
            frame.render_widget(
                Paragraph::new(entries)
                    .block(
                        Block::default()
                            .borders(Borders::ALL)
                            .title(format!("Browse: {}", browser.directory.display())),
                    )
                    .wrap(Wrap { trim: false }),
                chunks[1],
            );
        } else {
            self.render_screen(frame, chunks[1]);
        }
        let footer = match self.screen {
            Screen::Running => "Ctrl+C cancel/exit",
            Screen::Completed => {
                "Space play/pause  S stop  +/- volume  F filter  R rerun  Esc exit"
            }
            Screen::Filter => "Tab field  Up/Down rule  Space toggle  F5 apply  R reset  Esc back",
            Screen::Failed => "Esc exit",
            Screen::Input => "Enter parameters  F2 browse  Esc quit",
            Screen::Parameters => "Space cycle/toggle  F5 run  Tab next  Esc quit",
        };
        frame.render_widget(
            Paragraph::new(format!("{footer}  |  {}", self.message)).style(Style::default().fg(
                if self.screen == Screen::Failed {
                    Color::Red
                } else {
                    Color::DarkGray
                },
            )),
            chunks[2],
        );
    }

    fn render_screen(&self, frame: &mut Frame, area: ratatui::layout::Rect) {
        match self.screen {
            Screen::Input => {
                let lines = vec![
                    Line::from(format!(
                        "{} input: {}",
                        focus_marker(self.focus == FIELD_INPUT),
                        self.field(FIELD_INPUT)
                    )),
                    Line::from(format!(
                        "{} output: {}",
                        focus_marker(self.focus == FIELD_OUTPUT),
                        self.field(FIELD_OUTPUT)
                    )),
                    Line::from(format!("operation: {:?}", self.operation())),
                ];
                frame.render_widget(
                    Paragraph::new(lines)
                        .block(Block::default().borders(Borders::ALL).title("Input"))
                        .wrap(Wrap { trim: false }),
                    area,
                );
            }
            Screen::Parameters => {
                let labels = [
                    ("timing", FIELD_TIMING),
                    ("bpm", FIELD_BPM),
                    ("transpose", FIELD_TRANSPOSE),
                    ("audio_track", FIELD_TRACK),
                    ("start_seconds", FIELD_START),
                    ("end_seconds", FIELD_END),
                    ("preview_wav", FIELD_PREVIEW),
                    ("overwrite", FIELD_OVERWRITE),
                    ("cleaning_profile", FIELD_CLEANING_PROFILE),
                    ("min_confidence", FIELD_MIN_CONFIDENCE),
                    ("min_duration_ms", FIELD_MIN_DURATION),
                    ("retrigger_gap_ms", FIELD_RETRIGGER_GAP),
                    ("arrangement", FIELD_ARRANGEMENT),
                    ("onset_window_ms", FIELD_ONSET_WINDOW),
                    ("max_voices", FIELD_MAX_VOICES),
                ];
                let lines = labels
                    .iter()
                    .map(|(label, index)| {
                        Line::from(format!(
                            "{} {label}: {}",
                            focus_marker(self.focus == *index),
                            self.field(*index)
                        ))
                    })
                    .collect::<Vec<_>>();
                frame.render_widget(
                    Paragraph::new(lines)
                        .block(Block::default().borders(Borders::ALL).title("Parameters"))
                        .wrap(Wrap { trim: false }),
                    area,
                );
            }
            Screen::Filter => {
                let mut lines = vec![Line::from(
                    "Within one rule: all ranges AND. Across rules: any rule OR.",
                )];
                for (index, rule) in self.filter_rules.iter().enumerate() {
                    let selected = if index == self.filter_rule_index {
                        ">"
                    } else {
                        " "
                    };
                    let marker = |field: usize| {
                        if index == self.filter_rule_index && field == self.filter_field_index {
                            "["
                        } else {
                            ""
                        }
                    };
                    let end_marker = |field: usize| {
                        if index == self.filter_rule_index && field == self.filter_field_index {
                            "]"
                        } else {
                            ""
                        }
                    };
                    let enabled = if rule.enabled { "x" } else { " " };
                    let confidence = format!(
                        "{}conf {}{}..{}",
                        marker(1),
                        display_or(&rule.confidence_min, "min"),
                        display_or(&rule.confidence_max, "max"),
                        end_marker(2)
                    );
                    let duration = format!(
                        "{}dur {}{}..{}ms",
                        marker(3),
                        display_or(&rule.duration_min, "min"),
                        display_or(&rule.duration_max, "max"),
                        end_marker(4)
                    );
                    let velocity = format!(
                        "{}vel {}{}..{}",
                        marker(5),
                        display_or(&rule.velocity_min, "min"),
                        display_or(&rule.velocity_max, "max"),
                        end_marker(6)
                    );
                    let pitch = format!(
                        "{}pitch {}{}..{}",
                        marker(7),
                        display_or(&rule.pitch_min, "min"),
                        display_or(&rule.pitch_max, "max"),
                        end_marker(8)
                    );
                    lines.push(Line::from(format!(
                        "{selected} rule {} [{}] {confidence} | {duration} | {velocity} | {pitch}",
                        index + 1,
                        enabled
                    )));
                }
                if self.filter_field_index >= 7 {
                    let min = self.filter_rules[self.filter_rule_index].pitch_min.trim();
                    let max = self.filter_rules[self.filter_rule_index].pitch_max.trim();
                    let min_note = parse_pitch_value(min)
                        .ok()
                        .map(|pitch| {
                            format!(
                                "{} -> {}",
                                format_pitch(i64::from(pitch)),
                                mapped_key(pitch, self.filter_transpose)
                            )
                        })
                        .unwrap_or_default();
                    let max_note = parse_pitch_value(max)
                        .ok()
                        .map(|pitch| {
                            format!(
                                "{} -> {}",
                                format_pitch(i64::from(pitch)),
                                mapped_key(pitch, self.filter_transpose)
                            )
                        })
                        .unwrap_or_default();
                    lines.push(Line::from(format!("pitch preview: {min_note}  {max_note}")));
                }
                lines.push(Line::from(
                    "Range is inclusive. Missing min/max uses the full legal domain.",
                ));
                frame.render_widget(
                    Paragraph::new(lines)
                        .block(Block::default().borders(Borders::ALL).title("Note Filter"))
                        .wrap(Wrap { trim: false }),
                    area,
                );
            }
            Screen::Running => {
                let chunks = Layout::default()
                    .direction(Direction::Vertical)
                    .constraints([
                        Constraint::Length(3),
                        Constraint::Length(3),
                        Constraint::Min(3),
                    ])
                    .split(area);
                frame.render_widget(Paragraph::new(format!("stage: {}", self.stage)), chunks[0]);
                if let Some(fraction) = self.fraction {
                    frame.render_widget(
                        Gauge::default()
                            .gauge_style(Style::default().fg(Color::Cyan))
                            .ratio(fraction.clamp(0.0, 1.0)),
                        chunks[1],
                    );
                } else {
                    frame.render_widget(Paragraph::new("progress: unknown"), chunks[1]);
                }
                frame.render_widget(
                    Paragraph::new(self.warnings.join("\n"))
                        .block(Block::default().borders(Borders::ALL).title("Warnings")),
                    chunks[2],
                );
            }
            Screen::Completed => {
                let chunks = Layout::default()
                    .direction(Direction::Vertical)
                    .constraints([Constraint::Length(4), Constraint::Min(2)])
                    .split(area);
                let mut text = self.message.clone();
                if let Some(directory) = &self.result_directory {
                    text.push_str(&format!("\nresult: {}", directory.display()));
                }
                if let Some(playback) = &self.playback {
                    if let Some(error) = playback.error() {
                        text.push_str(&format!("\npreview unavailable: {error}"));
                    } else {
                        text.push_str(&format!("\npreview volume: {:.1}", self.playback_volume));
                    }
                } else if self.preview_path.is_none() {
                    text.push_str("\npreview: not generated");
                }
                let title = if self
                    .result_lines
                    .iter()
                    .any(|line| line.starts_with("warning ["))
                {
                    "Completed with warnings"
                } else {
                    "Completed"
                };
                frame.render_widget(
                    Paragraph::new(text).block(Block::default().borders(Borders::ALL).title(title)),
                    chunks[0],
                );
                frame.render_widget(
                    Paragraph::new(self.result_lines.join("\n"))
                        .block(
                            Block::default()
                                .borders(Borders::ALL)
                                .title("Result details"),
                        )
                        .scroll((self.result_scroll, 0))
                        .wrap(Wrap { trim: false }),
                    chunks[1],
                );
            }
            Screen::Failed => {
                let mut text = self.message.clone();
                if let Some(directory) = &self.result_directory {
                    text.push_str(&format!("\nresult: {}", directory.display()));
                }
                frame.render_widget(
                    Paragraph::new(text)
                        .block(Block::default().borders(Borders::ALL).title("Failed"))
                        .wrap(Wrap { trim: false }),
                    area,
                );
            }
        }
    }
}

fn focus_marker(active: bool) -> &'static str {
    if active { ">" } else { " " }
}

fn parse_cleaning_profile(value: &str) -> Result<CleaningProfile, CliError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => Ok(CleaningProfile::Auto),
        "solo" => Ok(CleaningProfile::Solo),
        "mix" => Ok(CleaningProfile::Mix),
        "strict" => Ok(CleaningProfile::Strict),
        _ => Err(CliError::InvalidArgument(
            "cleaning profile must be auto, solo, mix or strict".to_owned(),
        )),
    }
}

fn next_cleaning_profile(value: &str) -> &'static str {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => "solo",
        "solo" => "mix",
        "mix" => "strict",
        _ => "auto",
    }
}

fn parse_arrangement_profile(value: &str) -> Result<ArrangementProfile, CliError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "balanced" => Ok(ArrangementProfile::Balanced),
        "off" => Ok(ArrangementProfile::Off),
        _ => Err(CliError::InvalidArgument(
            "arrangement must be balanced or off".to_owned(),
        )),
    }
}

fn optional_range(
    minimum: &str,
    maximum: &str,
    lower: f64,
    upper: f64,
    name: &str,
) -> Result<Option<FilterRange>, CliError> {
    if minimum.trim().is_empty() && maximum.trim().is_empty() {
        return Ok(None);
    }
    let min = if minimum.trim().is_empty() {
        lower
    } else {
        minimum
            .trim()
            .parse::<f64>()
            .map_err(|_| CliError::InvalidArgument(format!("{name} minimum must be a number")))?
    };
    let max = if maximum.trim().is_empty() {
        upper
    } else {
        maximum
            .trim()
            .parse::<f64>()
            .map_err(|_| CliError::InvalidArgument(format!("{name} maximum must be a number")))?
    };
    if min < lower || max > upper || min > max {
        return Err(CliError::InvalidArgument(format!(
            "{name} must be within {lower}..{upper} and min <= max"
        )));
    }
    Ok(Some(FilterRange { min, max }))
}

fn optional_integer_range(
    minimum: &str,
    maximum: &str,
    lower: i64,
    upper: i64,
    name: &str,
) -> Result<Option<IntegerFilterRange>, CliError> {
    if minimum.trim().is_empty() && maximum.trim().is_empty() {
        return Ok(None);
    }
    let min = if minimum.trim().is_empty() {
        lower
    } else {
        minimum
            .trim()
            .parse::<i64>()
            .map_err(|_| CliError::InvalidArgument(format!("{name} minimum must be an integer")))?
    };
    let max = if maximum.trim().is_empty() {
        upper
    } else {
        maximum
            .trim()
            .parse::<i64>()
            .map_err(|_| CliError::InvalidArgument(format!("{name} maximum must be an integer")))?
    };
    if min < lower || max > upper || min > max {
        return Err(CliError::InvalidArgument(format!(
            "{name} must be within {lower}..{upper} and min <= max"
        )));
    }
    Ok(Some(IntegerFilterRange { min, max }))
}

fn optional_pitch_range(
    minimum: &str,
    maximum: &str,
) -> Result<Option<IntegerFilterRange>, CliError> {
    if minimum.trim().is_empty() && maximum.trim().is_empty() {
        return Ok(None);
    }
    let min = if minimum.trim().is_empty() {
        0
    } else {
        parse_pitch_value(minimum)?
    };
    let max = if maximum.trim().is_empty() {
        127
    } else {
        parse_pitch_value(maximum)?
    };
    if min > max {
        return Err(CliError::InvalidArgument(
            "pitch minimum must not exceed maximum".to_owned(),
        ));
    }
    Ok(Some(IntegerFilterRange {
        min: i64::from(min),
        max: i64::from(max),
    }))
}

fn parse_pitch_value(value: &str) -> Result<u8, CliError> {
    let trimmed = value.trim();
    if let Ok(number) = trimmed.parse::<i64>() {
        return u8::try_from(number)
            .ok()
            .filter(|pitch| *pitch <= 127)
            .ok_or_else(|| {
                CliError::InvalidArgument("pitch must be in MIDI range 0..127".to_owned())
            });
    }
    let normalized = trimmed.to_ascii_uppercase();
    let bytes = normalized.as_bytes();
    if bytes.len() < 2 || !bytes[0].is_ascii_alphabetic() {
        return Err(CliError::InvalidArgument(format!("invalid pitch: {value}")));
    }
    let semitone = match bytes[0] {
        b'C' => 0,
        b'D' => 2,
        b'E' => 4,
        b'F' => 5,
        b'G' => 7,
        b'A' => 9,
        b'B' => 11,
        _ => return Err(CliError::InvalidArgument(format!("invalid pitch: {value}"))),
    };
    let (accidental, octave_start) = if bytes.get(1) == Some(&b'#') {
        (1, 2)
    } else if bytes.get(1) == Some(&b'B') {
        (-1, 2)
    } else {
        (0, 1)
    };
    let octave = normalized[octave_start..]
        .parse::<i32>()
        .map_err(|_| CliError::InvalidArgument(format!("invalid pitch: {value}")))?;
    let pitch = (octave + 1) * 12 + semitone + accidental;
    u8::try_from(pitch)
        .ok()
        .filter(|pitch| *pitch <= 127)
        .ok_or_else(|| CliError::InvalidArgument(format!("pitch out of range: {value}")))
}

fn format_pitch(pitch: i64) -> String {
    const NAMES: [&str; 12] = [
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    ];
    let octave = pitch.div_euclid(12) - 1;
    let name = NAMES[pitch.rem_euclid(12) as usize];
    format!("{name}{octave}")
}

fn format_number(value: f64) -> String {
    if value.fract().abs() < f64::EPSILON {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

fn display_or<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value.trim()
    }
}

fn mapped_key(pitch: u8, transpose: i32) -> String {
    const KEYS: &str = "ZXCVBNMASDFGHJQWERTYU";
    let naturals = [0_i32, 2, 4, 5, 7, 9, 11];
    let mut value = i32::from(pitch) + transpose;
    if !naturals.contains(&value.rem_euclid(12)) {
        let lower = value - value.rem_euclid(12);
        let lower_natural = naturals
            .iter()
            .rev()
            .find(|candidate| lower + **candidate <= value)
            .copied()
            .unwrap_or(11);
        let mut upper = lower;
        let upper_natural = loop {
            upper += 1;
            if naturals.contains(&upper.rem_euclid(12)) {
                break upper.rem_euclid(12);
            }
        };
        let lower_pitch = lower + lower_natural;
        let upper_pitch = lower + upper_natural;
        value = if value - lower_pitch < upper_pitch - value {
            lower_pitch
        } else {
            upper_pitch
        };
    }
    while value < 48 {
        value += 12;
    }
    while value > 83 {
        value -= 12;
    }
    let natural_pitches: Vec<i32> = (48_i32..=83_i32)
        .filter(|candidate| naturals.contains(&(*candidate).rem_euclid(12)))
        .collect();
    match natural_pitches
        .iter()
        .position(|candidate| *candidate == value)
    {
        Some(index) => format!(
            "{}({})",
            KEYS.as_bytes()[index] as char,
            format_pitch(i64::from(value))
        ),
        None => format!("?({})", format_pitch(i64::from(value))),
    }
}

fn next_filter_directory(source: &Path) -> Result<PathBuf, CliError> {
    let parent = source
        .parent()
        .ok_or_else(|| CliError::InvalidArgument("result directory has no parent".to_owned()))?;
    let name = source
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            CliError::InvalidArgument("result directory name is not UTF-8".to_owned())
        })?;
    let base = name
        .rfind("-filter-")
        .filter(|index| {
            name[index + 8..]
                .chars()
                .all(|character| character.is_ascii_digit())
        })
        .map_or(name, |index| &name[..index]);
    for index in 1..=999 {
        let candidate = parent.join(format!("{base}-filter-{index:02}"));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    Err(CliError::InvalidArgument(
        "no available filter variant name remains".to_owned(),
    ))
}

fn next_arrangement_profile(value: &str) -> &'static str {
    match value.trim().to_ascii_lowercase().as_str() {
        "balanced" => "off",
        _ => "balanced",
    }
}

fn parse_bool(value: &str) -> Result<bool, CliError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Ok(true),
        "false" | "0" | "no" | "off" | "" => Ok(false),
        _ => Err(CliError::InvalidArgument(format!(
            "invalid boolean: {value}"
        ))),
    }
}

fn parse_optional_f64(value: &str, name: &str) -> Result<Option<f64>, CliError> {
    if value.trim().is_empty() {
        return Ok(None);
    }
    value
        .trim()
        .parse::<f64>()
        .map(Some)
        .map_err(|_| CliError::InvalidArgument(format!("{name} must be a number")))
}

fn parse_optional_u32(value: &str, name: &str) -> Result<Option<u32>, CliError> {
    if value.trim().is_empty() {
        return Ok(None);
    }
    value
        .trim()
        .parse::<u32>()
        .map(Some)
        .map_err(|_| CliError::InvalidArgument(format!("{name} must be a non-negative integer")))
}

fn parse_optional_u64(value: &str, name: &str) -> Result<Option<u64>, CliError> {
    if value.trim().is_empty() {
        return Ok(None);
    }
    value
        .trim()
        .parse::<u64>()
        .map(Some)
        .map_err(|_| CliError::InvalidArgument(format!("{name} must be a non-negative integer")))
}

fn parse_required_u64(value: &str, name: &str) -> Result<u64, CliError> {
    value
        .trim()
        .parse::<u64>()
        .map_err(|_| CliError::InvalidArgument(format!("{name} must be a non-negative integer")))
}

fn parse_required_u8(value: &str, name: &str) -> Result<u8, CliError> {
    value
        .trim()
        .parse::<u8>()
        .map_err(|_| CliError::InvalidArgument(format!("{name} must be a non-negative integer")))
}

fn parse_timing(value: &str) -> Result<Timing, CliError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => Ok(Timing::Auto),
        "preserve" => Ok(Timing::Preserve),
        "straight" => Ok(Timing::Straight),
        "triplet" => Ok(Timing::Triplet),
        _ => Err(CliError::InvalidArgument(
            "timing must be auto, preserve, straight or triplet".to_owned(),
        )),
    }
}

fn next_timing(value: &str, fallback: char) -> &'static str {
    let selected = if fallback.is_whitespace() {
        value.trim().to_ascii_lowercase()
    } else {
        match fallback.to_ascii_lowercase() {
            'p' => "preserve".to_owned(),
            's' => "straight".to_owned(),
            't' => "triplet".to_owned(),
            _ => "auto".to_owned(),
        }
    };
    match selected.as_str() {
        "auto" => "preserve",
        "preserve" => "straight",
        "straight" => "triplet",
        _ => "auto",
    }
}

fn parse_transpose(value: &str) -> Result<Transpose, CliError> {
    let value = value.trim();
    if value.eq_ignore_ascii_case("auto") {
        return Ok(Transpose::automatic());
    }
    let semitones = value
        .parse::<i32>()
        .map_err(|_| CliError::InvalidArgument("transpose must be auto or -48..48".to_owned()))?;
    if !(-48..=48).contains(&semitones) {
        return Err(CliError::InvalidArgument(
            "transpose must be auto or -48..48".to_owned(),
        ));
    }
    Ok(Transpose::Semitones(semitones))
}

fn next_transpose(value: &str) -> String {
    match parse_transpose(value) {
        Ok(Transpose::Auto(_)) => "0".to_owned(),
        Ok(Transpose::Semitones(value)) if value >= 12 => "auto".to_owned(),
        Ok(Transpose::Semitones(value)) => (value + 1).to_string(),
        Err(_) => "auto".to_owned(),
    }
}

struct TerminalGuard;

impl TerminalGuard {
    fn enter() -> Result<Self, CliError> {
        enable_raw_mode()?;
        if let Err(error) = execute!(
            std::io::stdout(),
            EnterAlternateScreen,
            EnableBracketedPaste
        ) {
            let _ = execute!(std::io::stdout(), LeaveAlternateScreen);
            let _ = disable_raw_mode();
            return Err(CliError::Output(error));
        }
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            std::io::stdout(),
            DisableBracketedPaste,
            LeaveAlternateScreen,
            Show
        );
    }
}

pub(crate) fn run() -> Result<(), CliError> {
    let _guard = TerminalGuard::enter()?;
    let backend = CrosstermBackend::new(std::io::stdout());
    let mut terminal = Terminal::new(backend)?;
    terminal.clear()?;
    let mut app = TuiApp::default();
    loop {
        terminal.draw(|frame| app.render(frame))?;
        app.poll_job();
        if event::poll(Duration::from_millis(50))? {
            match event::read()? {
                Event::Key(key) => {
                    if app.handle_key(key) {
                        break;
                    }
                }
                Event::Paste(text) => app.handle_paste(&text),
                _ => {}
            }
        }
    }
    app.request_cancel();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    #[test]
    fn form_uses_shared_option_builder() {
        let mut app = TuiApp::default();
        app.set_field(FIELD_INPUT, "song.mp4".to_owned());
        app.set_field(FIELD_OUTPUT, "out".to_owned());
        app.set_field(FIELD_TIMING, "straight".to_owned());
        app.set_field(FIELD_BPM, "120".to_owned());
        app.set_field(FIELD_TRANSPOSE, "-2".to_owned());
        app.set_field(FIELD_TRACK, "1".to_owned());
        app.set_field(FIELD_PREVIEW, "true".to_owned());
        app.set_field(FIELD_OVERWRITE, "true".to_owned());
        app.set_field(FIELD_CLEANING_PROFILE, "mix".to_owned());
        app.set_field(FIELD_MIN_CONFIDENCE, "0.55".to_owned());
        app.set_field(FIELD_MIN_DURATION, "125".to_owned());
        app.set_field(FIELD_RETRIGGER_GAP, "40".to_owned());
        app.set_field(FIELD_ARRANGEMENT, "off".to_owned());
        app.set_field(FIELD_ONSET_WINDOW, "175".to_owned());
        app.set_field(FIELD_MAX_VOICES, "3".to_owned());
        let cleaning = app.build_clean_options().unwrap();
        assert_eq!(cleaning.profile, CleaningProfile::Mix);
        assert_eq!(cleaning.min_confidence, Some(0.55));
        assert_eq!(cleaning.min_duration_us, Some(125_000));
        assert_eq!(cleaning.retrigger_gap_us, Some(40_000));
        let arrangement = app.build_arrangement_options().unwrap();
        assert_eq!(arrangement.profile, ArrangementProfile::Off);
        assert_eq!(arrangement.onset_window_us, 175_000);
        assert_eq!(arrangement.max_voices, 3);
        let options = app.build_options().unwrap();
        assert_eq!(options.timing, Some(Timing::Straight));
        assert_eq!(options.bpm, Some(120.0));
        assert_eq!(options.transpose, Some(Transpose::Semitones(-2)));
        assert_eq!(options.audio_track, Some(1));
        assert_eq!(options.preview_wav, Some(true));
        assert_eq!(options.overwrite, Some(true));
        assert_eq!(app.operation(), Operation::Transcribe);
    }

    #[test]
    fn midi_form_ignores_media_only_parameters() {
        let mut app = TuiApp::default();
        app.set_field(FIELD_INPUT, "song.mid".to_owned());
        app.set_field(FIELD_OUTPUT, "out".to_owned());
        app.set_field(FIELD_BPM, "120".to_owned());
        app.set_field(FIELD_TRACK, "3".to_owned());
        let options = app.build_options().unwrap();
        assert_eq!(app.operation(), Operation::ConvertMidi);
        assert_eq!(options.bpm, None);
        assert_eq!(options.audio_track, None);
    }

    #[test]
    fn release_events_do_not_duplicate_typed_text() {
        let mut app = TuiApp::default();
        app.handle_key(KeyEvent::new_with_kind(
            KeyCode::Char('a'),
            KeyModifiers::NONE,
            KeyEventKind::Press,
        ));
        app.handle_key(KeyEvent::new_with_kind(
            KeyCode::Char('a'),
            KeyModifiers::NONE,
            KeyEventKind::Release,
        ));
        assert_eq!(app.field(FIELD_INPUT), "a");
    }

    #[test]
    fn paste_fills_focused_path_and_strips_quotes() {
        let mut app = TuiApp::default();
        app.handle_paste("\"C:\\Music\\song.flac\"\n");
        assert_eq!(app.field(FIELD_INPUT), "C:\\Music\\song.flac");
        assert_eq!(app.field(FIELD_OUTPUT), "C:\\Music\\song-output");
    }

    #[test]
    fn input_enter_moves_to_output_then_parameters() {
        let mut app = TuiApp::default();
        app.set_field(FIELD_INPUT, "song.mid".to_owned());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.screen, Screen::Input);
        assert_eq!(app.focus, FIELD_OUTPUT);

        app.set_field(FIELD_OUTPUT, "out".to_owned());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.screen, Screen::Parameters);
        assert_eq!(app.focus, FIELD_TIMING);
    }

    #[test]
    fn result_view_shows_counts_warnings_and_preview() {
        use crate::jobs::{Artifact, ResultPayload};

        let root = std::env::temp_dir().join(format!("glt-tui-result-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            root.join("report.json"),
            r#"{"counts":{"input_notes":2,"output_notes":1,"dropped_notes":1,"mapped_keys":1,"replaced_semitones":0,"octave_folds":0,"duplicate_keys":0,"compatibility_collisions":0},"warnings":[{"code":"CLEANING_LOSS","message":"one note dropped"}],"artifacts":[{"kind":"preview_wav","relative_path":"preview.wav"}]}"#,
        )
        .unwrap();
        std::fs::write(root.join("preview.wav"), b"fake").unwrap();
        let outcome = JobOutcome {
            job_id: "local-test".to_owned(),
            result: ResultPayload {
                output_dir: root.clone(),
                report_path: PathBuf::from("report.json"),
                artifacts: vec![Artifact {
                    kind: "preview_wav".to_owned(),
                    relative_path: "preview.wav".to_owned(),
                    sha256: "a".repeat(64),
                    size_bytes: 4,
                }],
            },
        };
        let mut app = TuiApp::default();
        app.load_result(&outcome);
        assert!(
            app.result_lines
                .iter()
                .any(|line| line.contains("input_notes: 2"))
        );
        assert!(
            app.result_lines
                .iter()
                .any(|line| line.contains("CLEANING_LOSS"))
        );
        assert_eq!(app.preview_path, Some(root.join("preview.wav")));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn failed_job_can_return_to_parameters_for_retry() {
        let mut app = TuiApp {
            screen: Screen::Failed,
            ..TuiApp::default()
        };
        app.handle_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        assert_eq!(app.screen, Screen::Parameters);
        assert_eq!(app.focus, FIELD_TIMING);
    }

    #[test]
    fn filter_form_builds_grouped_ranges() {
        let mut app = TuiApp::default();
        app.filter_rules[0].enabled = true;
        app.filter_rules[0].confidence_min = "0.3".to_owned();
        app.filter_rules[0].confidence_max = "0.7".to_owned();
        app.filter_rules[0].duration_min = "100".to_owned();
        app.filter_rules[0].pitch_min = "C2".to_owned();
        app.filter_rules[0].pitch_max = "C5".to_owned();
        app.filter_rules[1].enabled = true;
        app.filter_rules[1].velocity_max = "60".to_owned();
        let spec = app.current_filter_spec().unwrap();
        assert_eq!(spec.rules.len(), 2);
        assert_eq!(spec.rules[0].pitch.as_ref().unwrap().min, 36);
        assert_eq!(spec.rules[0].pitch.as_ref().unwrap().max, 72);
        assert_eq!(spec.rules[1].velocity.as_ref().unwrap().min, 1);
    }

    #[test]
    fn filter_variant_names_increment_without_overwriting() {
        let root = std::env::temp_dir().join(format!("glt-filter-name-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("song-result");
        std::fs::create_dir_all(&source).unwrap();
        assert_eq!(
            next_filter_directory(&source).unwrap(),
            root.join("song-result-filter-01")
        );
        std::fs::create_dir_all(root.join("song-result-filter-01")).unwrap();
        assert_eq!(
            next_filter_directory(&root.join("song-result-filter-01")).unwrap(),
            root.join("song-result-filter-02")
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn filter_screen_requires_candidate_cache() {
        let mut app = TuiApp {
            screen: Screen::Completed,
            filter_cache_available: false,
            ..TuiApp::default()
        };
        app.open_filter();
        assert_eq!(app.screen, Screen::Completed);
        assert!(app.message.contains("candidate cache"));
    }

    #[test]
    fn v2_result_loads_filter_seed_and_cache() {
        use crate::jobs::{Artifact, ResultPayload};

        let root = std::env::temp_dir().join(format!("glt-tui-filter-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            root.join("report.json"),
            r#"{"schema_version":2,"parameters":{"filter":{"format_version":1,"rules":[{"enabled":true,"duration_ms":{"min":120,"max":1000}}]}},"counts":{},"warnings":[],"artifacts":[]}"#,
        )
        .unwrap();
        std::fs::write(
            root.join("score.candidates.json"),
            r#"{"format_version":1,"resolved_transpose":1}"#,
        )
        .unwrap();
        let outcome = JobOutcome {
            job_id: "local-filter".to_owned(),
            result: ResultPayload {
                output_dir: root.clone(),
                report_path: PathBuf::from("report.json"),
                artifacts: Vec::<Artifact>::new(),
            },
        };
        let mut app = TuiApp::default();
        app.load_result(&outcome);
        assert!(app.filter_cache_available);
        assert_eq!(app.filter_transpose, 1);
        assert_eq!(app.filter_seed.rules.len(), 1);
        app.open_filter();
        assert_eq!(app.screen, Screen::Filter);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn render_fits_supported_terminal_sizes() {
        for (width, height) in [(80, 24), (120, 40)] {
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            let app = TuiApp {
                screen: Screen::Filter,
                ..TuiApp::default()
            };
            terminal.draw(|frame| app.render(frame)).unwrap();
            assert_eq!(terminal.backend().buffer().area().width, width);
            assert_eq!(terminal.backend().buffer().area().height, height);
        }
    }

    #[test]
    fn poll_job_drains_progress_without_blocking() {
        let (sender, receiver) = mpsc::channel();
        let mut app = TuiApp {
            screen: Screen::Running,
            receiver: Some(receiver),
            ..TuiApp::default()
        };
        sender
            .send(UiJobEvent::Update(JobUpdate::Progress {
                stage: "mapping".to_owned(),
                fraction: None,
            }))
            .unwrap();
        app.poll_job();
        assert_eq!(app.stage, "mapping");
        assert_eq!(app.fraction, None);
    }
}
