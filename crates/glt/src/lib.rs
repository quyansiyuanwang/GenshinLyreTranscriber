//! Core crate surface for the `glt` application.

pub mod cli;
pub mod jobs;
pub mod preview;
pub mod protocol;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use super::VERSION;

    #[test]
    fn version_is_not_empty() {
        assert!(!VERSION.is_empty());
    }
}
