//! Device-independent playback control built on rodio.

use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use rodio::{Decoder, DeviceSinkBuilder, MixerDeviceSink, Player};
use thiserror::Error;

type BackendFactory =
    Arc<dyn Fn(&Path, f32) -> Result<Box<dyn PlaybackBackend>, PlaybackError> + Send + Sync>;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum PlaybackError {
    #[error("audio device is unavailable: {0}")]
    DeviceUnavailable(String),
    #[error("preview audio could not be decoded: {0}")]
    Decode(String),
    #[error("preview volume must be finite and in range 0..=1")]
    InvalidVolume,
    #[error("preview backend failed: {0}")]
    Backend(String),
}

pub trait PlaybackBackend: Send {
    fn play(&mut self) -> Result<(), PlaybackError>;
    fn pause(&mut self) -> Result<(), PlaybackError>;
    fn stop(&mut self) -> Result<(), PlaybackError>;
    fn set_volume(&mut self, volume: f32) -> Result<(), PlaybackError>;
    fn is_finished(&self) -> bool;
    fn position(&self) -> std::time::Duration {
        std::time::Duration::ZERO
    }
    fn seek(&mut self, _position: std::time::Duration) -> Result<(), PlaybackError> {
        Ok(())
    }
    fn is_paused(&self) -> bool {
        false
    }
}

pub struct PlaybackService {
    path: PathBuf,
    volume: f32,
    backend: Option<Box<dyn PlaybackBackend>>,
    factory: BackendFactory,
    last_error: Option<PlaybackError>,
}

impl PlaybackService {
    /// Create a playback service for a WAV file. Device failures are retained as
    /// unavailable state and never panic the caller.
    pub fn open_wav(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let factory: BackendFactory = Arc::new(|path, volume| {
            Ok(Box::new(RodioBackend::open(path, volume)?) as Box<dyn PlaybackBackend>)
        });
        Self::with_factory(path, 1.0, factory)
    }

    fn with_factory(path: PathBuf, volume: f32, factory: BackendFactory) -> Self {
        let mut service = Self {
            path,
            volume,
            backend: None,
            factory,
            last_error: None,
        };
        let _ = service.ensure_backend();
        service
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn volume(&self) -> f32 {
        self.volume
    }

    pub fn error(&self) -> Option<&PlaybackError> {
        self.last_error.as_ref()
    }

    pub fn is_available(&self) -> bool {
        self.backend.is_some()
    }

    pub fn is_finished(&self) -> bool {
        self.backend
            .as_ref()
            .is_none_or(|backend| backend.is_finished())
    }

    pub fn play(&mut self) -> Result<(), PlaybackError> {
        self.ensure_backend()?;
        if self.backend.is_none() {
            return Err(self.unavailable_error());
        }
        let result = self
            .backend
            .as_mut()
            .expect("backend was checked above")
            .play();
        self.store_result(result)
    }

    pub fn pause(&mut self) -> Result<(), PlaybackError> {
        if self.backend.is_none() {
            return Err(self.unavailable_error());
        }
        let result = self
            .backend
            .as_mut()
            .expect("backend was checked above")
            .pause();
        self.store_result(result)
    }

    /// Stop playback and release the decoder and OS audio stream.
    pub fn stop(&mut self) -> Result<(), PlaybackError> {
        let Some(mut backend) = self.backend.take() else {
            return Ok(());
        };
        let result = backend.stop();
        drop(backend);
        self.store_result(result)
    }

    pub fn set_volume(&mut self, volume: f32) -> Result<(), PlaybackError> {
        if !volume.is_finite() || !(0.0..=1.0).contains(&volume) {
            return Err(PlaybackError::InvalidVolume);
        }
        self.volume = volume;
        let Some(backend) = self.backend.as_mut() else {
            return Ok(());
        };
        let result = backend.set_volume(volume);
        self.store_result(result)
    }

    pub fn position(&self) -> std::time::Duration {
        self.backend
            .as_ref()
            .map_or(std::time::Duration::ZERO, |backend| backend.position())
    }

    pub fn seek(&mut self, position: std::time::Duration) -> Result<(), PlaybackError> {
        if self.backend.is_none() {
            self.ensure_backend()?;
        }
        let result = self
            .backend
            .as_mut()
            .expect("backend was checked above")
            .seek(position);
        self.store_result(result)
    }

    pub fn is_paused(&self) -> bool {
        self.backend
            .as_ref()
            .is_none_or(|backend| backend.is_paused())
    }

    fn ensure_backend(&mut self) -> Result<(), PlaybackError> {
        if self.backend.is_some() {
            return Ok(());
        }
        match (self.factory)(&self.path, self.volume) {
            Ok(backend) => {
                self.backend = Some(backend);
                self.last_error = None;
                Ok(())
            }
            Err(error) => {
                self.last_error = Some(error.clone());
                Err(error)
            }
        }
    }

    fn unavailable_error(&self) -> PlaybackError {
        self.last_error.clone().unwrap_or_else(|| {
            PlaybackError::DeviceUnavailable("no preview backend is loaded".to_owned())
        })
    }

    fn store_result(&mut self, result: Result<(), PlaybackError>) -> Result<(), PlaybackError> {
        if let Err(error) = &result {
            self.last_error = Some(error.clone());
        } else {
            self.last_error = None;
        }
        result
    }
}

pub struct RodioBackend {
    player: Player,
    _device: MixerDeviceSink,
}

impl RodioBackend {
    fn open(path: &Path, volume: f32) -> Result<Self, PlaybackError> {
        let mut device = DeviceSinkBuilder::open_default_sink()
            .map_err(|error| PlaybackError::DeviceUnavailable(error.to_string()))?;
        device.log_on_drop(false);
        let player = Player::connect_new(device.mixer());
        let file = File::open(path).map_err(|error| PlaybackError::Decode(error.to_string()))?;
        let source = Decoder::try_from(BufReader::new(file))
            .map_err(|error| PlaybackError::Decode(error.to_string()))?;
        player.set_volume(volume);
        player.append(source);
        player.pause();
        Ok(Self {
            player,
            _device: device,
        })
    }
}

impl PlaybackBackend for RodioBackend {
    fn play(&mut self) -> Result<(), PlaybackError> {
        self.player.play();
        Ok(())
    }

    fn pause(&mut self) -> Result<(), PlaybackError> {
        self.player.pause();
        Ok(())
    }

    fn stop(&mut self) -> Result<(), PlaybackError> {
        self.player.stop();
        Ok(())
    }

    fn set_volume(&mut self, volume: f32) -> Result<(), PlaybackError> {
        self.player.set_volume(volume);
        Ok(())
    }

    fn is_finished(&self) -> bool {
        self.player.empty()
    }

    fn position(&self) -> std::time::Duration {
        self.player.get_pos()
    }

    fn seek(&mut self, position: std::time::Duration) -> Result<(), PlaybackError> {
        self.player
            .try_seek(position)
            .map_err(|error| PlaybackError::Backend(error.to_string()))
    }

    fn is_paused(&self) -> bool {
        self.player.is_paused()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    #[derive(Default)]
    struct FakeState {
        calls: Mutex<Vec<&'static str>>,
        dropped: AtomicBool,
        volume: Mutex<f32>,
    }

    struct FakeBackend {
        state: Arc<FakeState>,
    }

    impl PlaybackBackend for FakeBackend {
        fn play(&mut self) -> Result<(), PlaybackError> {
            self.state.calls.lock().unwrap().push("play");
            Ok(())
        }

        fn pause(&mut self) -> Result<(), PlaybackError> {
            self.state.calls.lock().unwrap().push("pause");
            Ok(())
        }

        fn stop(&mut self) -> Result<(), PlaybackError> {
            self.state.calls.lock().unwrap().push("stop");
            Ok(())
        }

        fn set_volume(&mut self, volume: f32) -> Result<(), PlaybackError> {
            self.state.calls.lock().unwrap().push("volume");
            *self.state.volume.lock().unwrap() = volume;
            Ok(())
        }

        fn is_finished(&self) -> bool {
            false
        }
    }

    impl Drop for FakeBackend {
        fn drop(&mut self) {
            self.state.dropped.store(true, Ordering::SeqCst);
        }
    }

    fn fake_factory(state: Arc<FakeState>, opens: Arc<AtomicUsize>) -> BackendFactory {
        Arc::new(move |_path, _volume| {
            opens.fetch_add(1, Ordering::SeqCst);
            Ok(Box::new(FakeBackend {
                state: Arc::clone(&state),
            }))
        })
    }

    #[test]
    fn controls_play_pause_stop_and_volume() {
        let state = Arc::new(FakeState::default());
        let opens = Arc::new(AtomicUsize::new(0));
        let mut service = PlaybackService::with_factory(
            PathBuf::from("preview.wav"),
            1.0,
            fake_factory(Arc::clone(&state), Arc::clone(&opens)),
        );

        service.play().unwrap();
        service.pause().unwrap();
        service.play().unwrap();
        service.set_volume(0.4).unwrap();
        assert_eq!(service.volume(), 0.4);
        assert_eq!(
            *state.calls.lock().unwrap(),
            ["play", "pause", "play", "volume"]
        );
        assert_eq!(opens.load(Ordering::SeqCst), 1);

        service.stop().unwrap();
        assert!(state.dropped.load(Ordering::SeqCst));
        assert_eq!(state.calls.lock().unwrap().last(), Some(&"stop"));
    }

    #[test]
    fn device_failure_keeps_service_alive_and_volume_settable() {
        let factory: BackendFactory = Arc::new(|_path, _volume| {
            Err(PlaybackError::DeviceUnavailable("no output".to_owned()))
        });
        let mut service = PlaybackService::with_factory(PathBuf::from("preview.wav"), 1.0, factory);

        assert!(!service.is_available());
        assert!(matches!(
            service.error(),
            Some(PlaybackError::DeviceUnavailable(_))
        ));
        assert!(matches!(
            service.play(),
            Err(PlaybackError::DeviceUnavailable(_))
        ));
        service.set_volume(0.5).unwrap();
        assert_eq!(service.volume(), 0.5);
        service.stop().unwrap();
        assert!(service.is_finished());
    }

    #[test]
    fn stop_releases_backend_and_play_can_reopen_it() {
        let state = Arc::new(FakeState::default());
        let opens = Arc::new(AtomicUsize::new(0));
        let mut service = PlaybackService::with_factory(
            PathBuf::from("preview.wav"),
            1.0,
            fake_factory(Arc::clone(&state), Arc::clone(&opens)),
        );

        service.stop().unwrap();
        assert!(state.dropped.load(Ordering::SeqCst));
        service.play().unwrap();
        assert_eq!(opens.load(Ordering::SeqCst), 2);
        assert_eq!(state.calls.lock().unwrap().last(), Some(&"play"));
    }

    #[test]
    fn rejects_invalid_volume() {
        let factory: BackendFactory = Arc::new(|_path, _volume| {
            Err(PlaybackError::DeviceUnavailable("no output".to_owned()))
        });
        let mut service = PlaybackService::with_factory(PathBuf::from("preview.wav"), 1.0, factory);
        assert_eq!(
            service.set_volume(f32::NAN),
            Err(PlaybackError::InvalidVolume)
        );
        assert_eq!(service.set_volume(1.1), Err(PlaybackError::InvalidVolume));
        assert_eq!(service.volume(), 1.0);
    }
}
