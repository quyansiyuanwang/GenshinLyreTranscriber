use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

pub const PROJECT_FORMAT_VERSION: u8 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectSource {
    pub path: PathBuf,
    pub sha256: Option<String>,
    pub size_bytes: Option<u64>,
    pub modified_unix_ms: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectRevision {
    pub id: String,
    pub kind: String,
    pub relative_path: String,
    pub parent_id: Option<String>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectDocument {
    pub format_version: u8,
    pub project_id: String,
    pub name: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub source: Option<ProjectSource>,
    pub revisions: Vec<ProjectRevision>,
}

impl ProjectDocument {
    fn new(name: String, source: Option<ProjectSource>) -> Self {
        let now = Utc::now();
        Self {
            format_version: PROJECT_FORMAT_VERSION,
            project_id: Uuid::new_v4().to_string(),
            name,
            created_at: now,
            updated_at: now,
            source,
            revisions: Vec::new(),
        }
    }

    fn validate(&self) -> Result<(), String> {
        if self.format_version != PROJECT_FORMAT_VERSION {
            return Err(format!(
                "unsupported project format version: {}",
                self.format_version
            ));
        }
        if self.project_id.trim().is_empty() || self.name.trim().is_empty() {
            return Err("project id and name must not be empty".to_owned());
        }
        for revision in &self.revisions {
            let relative = Path::new(&revision.relative_path);
            if relative.is_absolute() || relative.components().any(|part| part.as_os_str() == "..")
            {
                return Err(format!("unsafe revision path: {}", revision.relative_path));
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
struct ProjectLock {
    path: PathBuf,
}

impl ProjectLock {
    fn acquire(project_path: &Path) -> Result<Self, String> {
        let path = sidecar(project_path, "lock");
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| {
                format!(
                    "project is already open or lock cannot be created at {}: {error}",
                    path.display()
                )
            })?;
        Ok(Self { path })
    }
}

impl Drop for ProjectLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[derive(Debug)]
pub struct OpenProject {
    path: PathBuf,
    document: ProjectDocument,
    _lock: ProjectLock,
}

impl OpenProject {
    pub fn document(&self) -> &ProjectDocument {
        &self.document
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
}

pub fn create(path: &Path, name: String, source: Option<PathBuf>) -> Result<OpenProject, String> {
    validate_project_path(path)?;
    if path.exists() {
        return Err(format!("project already exists: {}", path.display()));
    }
    let lock = ProjectLock::acquire(path)?;
    let source = source.map(source_metadata).transpose()?;
    let document = ProjectDocument::new(name, source);
    ensure_project_directories(path)?;
    write_document(path, &document)?;
    Ok(OpenProject {
        path: path.to_path_buf(),
        document,
        _lock: lock,
    })
}

pub fn open(path: &Path) -> Result<OpenProject, String> {
    validate_project_path(path)?;
    if !path.is_file() {
        return Err(format!("project does not exist: {}", path.display()));
    }
    let lock = ProjectLock::acquire(path)?;
    let text = fs::read_to_string(path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    let document: ProjectDocument =
        serde_json::from_str(&text).map_err(|error| format!("invalid project JSON: {error}"))?;
    document.validate()?;
    ensure_project_directories(path)?;
    Ok(OpenProject {
        path: path.to_path_buf(),
        document,
        _lock: lock,
    })
}

pub fn save(project: &mut OpenProject, name: Option<String>) -> Result<(), String> {
    if let Some(name) = name {
        if name.trim().is_empty() {
            return Err("project name must not be empty".to_owned());
        }
        project.document.name = name;
    }
    project.document.updated_at = Utc::now();
    project.document.validate()?;
    write_document(&project.path, &project.document)
}

pub fn relink_source(project: &mut OpenProject, source: PathBuf) -> Result<(), String> {
    project.document.source = Some(source_metadata(source)?);
    project.document.updated_at = Utc::now();
    write_document(&project.path, &project.document)
}

pub fn add_revision(
    project: &mut OpenProject,
    kind: String,
    relative_path: String,
    parent_id: Option<String>,
) -> Result<ProjectRevision, String> {
    let relative = Path::new(&relative_path);
    if relative.is_absolute() || relative.components().any(|part| part.as_os_str() == "..") {
        return Err(format!("unsafe revision path: {relative_path}"));
    }
    let revision = ProjectRevision {
        id: Uuid::new_v4().to_string(),
        kind,
        relative_path,
        parent_id,
        created_at: Utc::now(),
    };
    project.document.revisions.push(revision.clone());
    project.document.updated_at = Utc::now();
    write_document(&project.path, &project.document)?;
    Ok(revision)
}

pub fn add_revision_absolute(
    project: &mut OpenProject,
    kind: String,
    absolute_path: PathBuf,
    parent_id: Option<String>,
) -> Result<ProjectRevision, String> {
    let root = project
        .path
        .parent()
        .ok_or_else(|| "project path has no parent".to_owned())?;
    let relative = absolute_path
        .strip_prefix(root)
        .map_err(|_| "revision output must be inside the project directory".to_owned())?;
    let relative_path = relative.to_string_lossy().replace('\\', "/");
    add_revision(project, kind, relative_path, parent_id)
}

pub fn assets_directory(project_path: &Path) -> PathBuf {
    sidecar(project_path, "assets")
}

pub fn revisions_directory(project_path: &Path) -> PathBuf {
    sidecar(project_path, "revisions")
}

fn validate_project_path(path: &Path) -> Result<(), String> {
    if path.extension().and_then(|value| value.to_str()) != Some("gltproj") {
        return Err("project path must use the .gltproj extension".to_owned());
    }
    if path.parent().is_none() {
        return Err("project path must have a parent directory".to_owned());
    }
    Ok(())
}

fn ensure_project_directories(path: &Path) -> Result<(), String> {
    fs::create_dir_all(assets_directory(path))
        .map_err(|error| format!("cannot create project assets: {error}"))?;
    fs::create_dir_all(revisions_directory(path))
        .map_err(|error| format!("cannot create project revisions: {error}"))?;
    Ok(())
}

fn write_document(path: &Path, document: &ProjectDocument) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "project path has no parent".to_owned())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("cannot create project parent directory: {error}"))?;
    let partial = path.with_extension("gltproj.partial");
    let mut file = File::create(&partial)
        .map_err(|error| format!("cannot create {}: {error}", partial.display()))?;
    serde_json::to_writer_pretty(&mut file, document)
        .map_err(|error| format!("cannot serialize project: {error}"))?;
    file.write_all(b"\n")
        .map_err(|error| format!("cannot write project: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("cannot flush project: {error}"))?;
    drop(file);
    if path.exists() {
        fs::remove_file(path).map_err(|error| format!("cannot replace project: {error}"))?;
    }
    fs::rename(&partial, path).map_err(|error| format!("cannot publish project: {error}"))
}

fn source_metadata(path: PathBuf) -> Result<ProjectSource, String> {
    let resolved = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve source {}: {error}", path.display()))?;
    if !resolved.is_file() {
        return Err(format!("source is not a file: {}", resolved.display()));
    }
    let metadata =
        fs::metadata(&resolved).map_err(|error| format!("cannot read source metadata: {error}"))?;
    Ok(ProjectSource {
        sha256: Some(sha256_file(&resolved)?),
        size_bytes: Some(metadata.len()),
        modified_unix_ms: metadata
            .modified()
            .ok()
            .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
            .and_then(|value| u64::try_from(value.as_millis()).ok()),
        path: resolved,
    })
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let file = File::open(path).map_err(|error| format!("cannot open source: {error}"))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    // Project commands may run on Tauri worker threads; avoid a one-megabyte stack frame.
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|error| format!("cannot hash source: {error}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn sidecar(project_path: &Path, suffix: &str) -> PathBuf {
    let mut name = project_path
        .file_name()
        .map(|value| value.to_os_string())
        .unwrap_or_else(|| "project.gltproj".into());
    name.push(format!(".{suffix}"));
    project_path.with_file_name(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path() -> PathBuf {
        std::env::temp_dir().join(format!("glt-project-{}.gltproj", Uuid::new_v4()))
    }

    #[test]
    fn project_round_trip_and_lock_conflict() {
        let path = temp_path();
        let mut project = create(&path, "demo".to_owned(), None).unwrap();
        assert!(path.is_file());
        assert!(assets_directory(&path).is_dir());

        let conflict = open(&path).unwrap_err();
        assert!(conflict.contains("already open"));

        add_revision(
            &mut project,
            "initial".to_owned(),
            "revisions/edit-00".to_owned(),
            None,
        )
        .unwrap();
        save(&mut project, Some("renamed".to_owned())).unwrap();
        drop(project);

        let reopened = open(&path).unwrap();
        assert_eq!(reopened.document().name, "renamed");
        assert_eq!(reopened.document().revisions.len(), 1);
        drop(reopened);

        let _ = fs::remove_file(&path);
        let _ = fs::remove_dir_all(assets_directory(&path));
        let _ = fs::remove_dir_all(revisions_directory(&path));
    }

    #[test]
    fn hash_is_stable_and_metadata_is_relative_to_source() {
        let source = std::env::temp_dir().join(format!("glt-source-{}.bin", Uuid::new_v4()));
        fs::write(&source, b"audio").unwrap();
        let metadata = source_metadata(source.clone()).unwrap();
        assert_eq!(
            metadata.sha256.as_deref(),
            Some("6ed8919ce20490a5e3ad8630a4fab69475297abd07db73918dd5f36fcfaeb11b")
        );
        assert_eq!(metadata.size_bytes, Some(5));
        let _ = fs::remove_file(source);
    }

    #[test]
    fn absolute_revision_path_is_recorded_relative_to_project() {
        let path = temp_path();
        let mut project = create(&path, "revision".to_owned(), None).unwrap();
        let output = assets_directory(&path).join("edit-01");
        fs::create_dir_all(&output).unwrap();
        let revision =
            add_revision_absolute(&mut project, "performance-edit".to_owned(), output, None)
                .unwrap();
        assert!(revision.relative_path.contains("edit-01"));
        assert!(!Path::new(&revision.relative_path).is_absolute());
        drop(project);
        let _ = fs::remove_file(&path);
        let _ = fs::remove_dir_all(assets_directory(&path));
        let _ = fs::remove_dir_all(revisions_directory(&path));
    }
}
