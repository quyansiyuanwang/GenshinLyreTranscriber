//! Minimal terminal UI for choosing inputs, parameters and running one job.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crossterm::cursor::Show;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyModifiers};
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
    CleaningOptions, CliError, JobOutcome, JobUpdate, build_cleaning_options, build_job_options,
    run_job_with_cancel,
};
use crate::jobs::{Operation, StartOptions, Timing, Transpose};
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
const FIELD_MIN_CONFIDENCE: usize = 10;
const FIELD_MIN_DURATION: usize = 11;
const FIELD_RETRIGGER_GAP: usize = 12;
const FIELD_COUNT: usize = 13;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Screen {
    Input,
    Parameters,
    Running,
    Completed,
    Failed,
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
            FIELD_PREVIEW | FIELD_OVERWRITE => "false".to_owned(),
            FIELD_MIN_CONFIDENCE => "0.2".to_owned(),
            FIELD_MIN_DURATION => "50".to_owned(),
            FIELD_RETRIGGER_GAP => "30".to_owned(),
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
            parse_optional_f64(self.field(FIELD_MIN_CONFIDENCE), "min-confidence")?,
            parse_optional_u64(self.field(FIELD_MIN_DURATION), "min-duration-ms")?,
            parse_optional_u64(self.field(FIELD_RETRIGGER_GAP), "retrigger-gap-ms")?,
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
        let operation = self.operation();
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

    fn handle_key(&mut self, key: KeyEvent) -> bool {
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
        if self.screen == Screen::Completed {
            match key.code {
                KeyCode::Char(' ') => self.toggle_playback(),
                KeyCode::Char('s') => self.stop_playback(),
                KeyCode::Char('+' | '=') => self.adjust_volume(0.1),
                KeyCode::Char('-') => self.adjust_volume(-0.1),
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
                    || self.focus == FIELD_MIN_CONFIDENCE
                    || self.focus == FIELD_MIN_DURATION
                    || self.focus == FIELD_RETRIGGER_GAP
                {
                    self.fields[self.focus].pop();
                }
            }
            KeyCode::Char(character) => {
                if self.focus == FIELD_TIMING {
                    self.fields[self.focus] =
                        next_timing(self.field(self.focus), character).to_owned();
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
            Screen::Completed => "Space play/pause  S stop  +/- volume  R rerun  Esc exit",
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
                    ("min_confidence", FIELD_MIN_CONFIDENCE),
                    ("min_duration_ms", FIELD_MIN_DURATION),
                    ("retrigger_gap_ms", FIELD_RETRIGGER_GAP),
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
        if let Err(error) = execute!(std::io::stdout(), EnterAlternateScreen) {
            let _ = disable_raw_mode();
            return Err(CliError::Output(error));
        }
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(std::io::stdout(), LeaveAlternateScreen, Show);
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
            let Event::Key(key) = event::read()? else {
                continue;
            };
            if app.handle_key(key) {
                break;
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
        app.set_field(FIELD_MIN_CONFIDENCE, "0.55".to_owned());
        app.set_field(FIELD_MIN_DURATION, "125".to_owned());
        app.set_field(FIELD_RETRIGGER_GAP, "40".to_owned());
        let cleaning = app.build_clean_options().unwrap();
        assert_eq!(cleaning.min_confidence, 0.55);
        assert_eq!(cleaning.min_duration_us, 125_000);
        assert_eq!(cleaning.retrigger_gap_us, 40_000);
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
    fn render_fits_supported_terminal_sizes() {
        for (width, height) in [(80, 24), (120, 40)] {
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            let app = TuiApp::default();
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
