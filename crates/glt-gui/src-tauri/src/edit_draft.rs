//! Desktop-only recovery drafts. Never writes to a published result directory.
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};
use uuid::Uuid;

const MAX_BYTES: u64 = 64 * 1024 * 1024;
const SAFE_TIME: u64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EditorState {
    pub selected_ids: Vec<String>,
    pub view_start_us: u64,
    pub view_duration_us: u64,
    pub snap_to_beat: bool,
    pub tool: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EditDraft {
    pub format_version: u8,
    pub source_result: String,
    pub base_revision_id: String,
    pub base_sha256: String,
    pub updated_at: String,
    pub performance: Value,
    pub editor_state: EditorState,
}

#[derive(Serialize)]
pub struct EditSession {
    pub format_version: u8,
    pub source_result: String,
    pub baseline: Option<Value>,
    pub base_sha256: Option<String>,
    pub base_revision_id: Option<String>,
    pub draft: Option<EditDraft>,
    pub draft_sha256: Option<String>,
    pub can_restore: bool,
    pub warning: Option<String>,
}

#[derive(Serialize)]
pub struct SavedDraft {
    pub format_version: u8,
    pub draft_sha256: String,
    pub updated_at: String,
}

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn read_limited(path: &Path) -> Result<Vec<u8>, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("草稿或基准文件超过 64MiB 限制，未修改已有草稿".into());
    }
    Ok(bytes)
}

fn source_identity(path: &Path) -> Result<String, String> {
    let absolute = fs::canonicalize(path)
        .or_else(|_| std::path::absolute(path))
        .map_err(|e| e.to_string())?;
    let text = absolute
        .to_string_lossy()
        .trim_start_matches(r"\\?\")
        .replace('\\', "/");
    Ok(if cfg!(windows) {
        text.to_lowercase()
    } else {
        text
    })
}

fn draft_path(root: &Path, source: &str) -> PathBuf {
    root.join(format!("{}.json", sha(source.as_bytes())))
}

// The OS releases this lock even on crash. Keep the lock file: deleting it could
// allow concurrent handles to lock different files with the same name.
fn lock(root: &Path, source: &str) -> Result<File, String> {
    fs::create_dir_all(root).map_err(|e| e.to_string())?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(draft_path(root, source).with_extension("lock"))
        .map_err(|e| e.to_string())?;
    file.try_lock()
        .map_err(|_| "另一窗口正在操作此草稿，请稍后重试".to_owned())?;
    Ok(file)
}

// Packaged-parent AppData virtualization may redirect individual files while
// canonicalizing the directory still reports a different volume. Resolve an
// existing, locked regular file first; staging and publication then share its
// actual directory. Never replace atomic publication with a copy over the draft.
fn physical_root(root: &Path, source: &str) -> Result<PathBuf, String> {
    let path = draft_path(root, source).with_extension("lock");
    if fs::symlink_metadata(&path)
        .map_err(|e| e.to_string())?
        .file_type()
        .is_symlink()
    {
        return Err("草稿锁不能是符号链接".into());
    }
    fs::canonicalize(path)
        .map_err(|e| e.to_string())?
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "无法确定草稿数据目录".into())
}

fn validate_performance(value: &Value) -> Result<(), String> {
    static VALIDATOR: OnceLock<jsonschema::Validator> = OnceLock::new();
    let validator = VALIDATOR.get_or_init(|| {
        let schema: Value = serde_json::from_str(include_str!(
            "../../../../schemas/performance-v1.schema.json"
        ))
        .expect("embedded performance schema");
        jsonschema::validator_for(&schema).expect("valid performance schema")
    });
    validator
        .validate(value)
        .map_err(|e| format!("Performance 无效：{e}"))?;
    let duration = value["duration_us"].as_u64().ok_or("缺少时长")?;
    let mut ids = std::collections::HashSet::new();
    for note in value["notes"].as_array().ok_or("缺少音符")? {
        let id = note["id"].as_str().ok_or("缺少音符编号")?;
        if !ids.insert(id)
            || note["start_us"].as_u64() >= note["end_us"].as_u64()
            || note["end_us"].as_u64().unwrap_or(u64::MAX) > duration
        {
            return Err("音符编号重复或时间越界".into());
        }
    }
    Ok(())
}

fn validate(draft: &EditDraft) -> Result<(), String> {
    if draft.format_version != 1
        || draft.source_result.is_empty()
        || draft.base_revision_id.is_empty()
        || draft.base_sha256.len() != 64
        || !draft
            .base_sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || !["select", "add"].contains(&draft.editor_state.tool.as_str())
        || draft.editor_state.view_start_us > SAFE_TIME
        || draft.editor_state.view_duration_us > SAFE_TIME
        || draft.editor_state.view_duration_us == 0
        || draft.editor_state.selected_ids.len() > 100_000
    {
        return Err("不支持的草稿版本或非法编辑状态".into());
    }
    validate_performance(&draft.performance)?;
    if draft.performance["revision"]["id"].as_str() != Some(draft.base_revision_id.as_str()) {
        return Err("草稿 revision 与基准不一致".into());
    }
    Ok(())
}

fn baseline(source: &str) -> Result<(Value, String, String), String> {
    let bytes = read_limited(&Path::new(source).join("performance.json"))?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    validate_performance(&value)?;
    let revision = value["revision"]["id"]
        .as_str()
        .ok_or("基准 revision 缺失")?
        .to_owned();
    Ok((value, sha(&bytes), revision))
}

fn stored(root: &Path, source: &str) -> Result<Option<(Vec<u8>, String)>, String> {
    let path = draft_path(root, source);
    match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.to_string()),
        Ok(meta) if !meta.file_type().is_file() => Err("草稿路径不是常规文件，拒绝替换".into()),
        Ok(_) => {
            let bytes = read_limited(&path)?;
            let hash = sha(&bytes);
            Ok(Some((bytes, hash)))
        }
    }
}

fn load(root: &Path, result: &Path) -> Result<EditSession, String> {
    let source = source_identity(result)?;
    let _lock = lock(root, &source)?;
    let physical = physical_root(root, &source)?;
    let root = physical.as_path();
    let saved = stored(root, &source)?;
    let mut session = EditSession {
        format_version: 1,
        source_result: source.clone(),
        baseline: None,
        base_sha256: None,
        base_revision_id: None,
        draft: None,
        draft_sha256: saved.as_ref().map(|(_, hash)| hash.clone()),
        can_restore: false,
        warning: None,
    };
    if let Some((bytes, _)) = saved {
        let parsed = serde_json::from_slice::<EditDraft>(&bytes)
            .map_err(|e| e.to_string())
            .and_then(|draft| {
                validate(&draft)?;
                chrono::DateTime::parse_from_rfc3339(&draft.updated_at)
                    .map_err(|e| e.to_string())?;
                Ok(draft)
            });
        match parsed {
            Ok(draft) => session.draft = Some(draft),
            Err(reason) => {
                session.warning = Some(format!(
                    "恢复草稿无法读取，原文件保留在 {}：{reason}",
                    draft_path(root, &source).display()
                ))
            }
        }
    }
    match baseline(&source) {
        Ok((value, hash, revision)) => {
            session.can_restore = session.draft.as_ref().is_some_and(|d| {
                d.source_result == source && d.base_sha256 == hash && d.base_revision_id == revision
            });
            if session.draft.is_some() && !session.can_restore {
                session.warning = Some(
                    "基准内容或 revision 已变化；原草稿已保留，不能自动套用。请恢复原基准后重试。"
                        .into(),
                );
            }
            session.baseline = Some(value);
            session.base_sha256 = Some(hash);
            session.base_revision_id = Some(revision);
        }
        Err(reason) => session.warning = Some(format!("基准缺失或不可用，草稿保持不变：{reason}")),
    }
    Ok(session)
}

fn check_expected(root: &Path, source: &str, expected: Option<&str>) -> Result<(), String> {
    let actual = stored(root, source)?.map(|(_, hash)| hash);
    if actual.as_deref() != expected {
        return Err("草稿已被其他操作更新；请重新打开结果检查，未覆盖或删除任何内容".into());
    }
    Ok(())
}

fn save(root: &Path, mut draft: EditDraft, expected: Option<&str>) -> Result<SavedDraft, String> {
    validate(&draft)?;
    let source = source_identity(Path::new(&draft.source_result))?;
    let _lock = lock(root, &source)?;
    let physical = physical_root(root, &source)?;
    let root = physical.as_path();
    check_expected(root, &source, expected)?;
    let (_, hash, revision) = baseline(&source)?;
    if hash != draft.base_sha256 || revision != draft.base_revision_id {
        return Err("基准已改变，草稿未覆盖；请保留当前编辑并恢复基准文件".into());
    }
    draft.source_result = source.clone();
    draft.updated_at = Utc::now().to_rfc3339();
    let bytes = serde_json::to_vec(&draft).map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("草稿超过 64MiB，未覆盖原草稿".into());
    }
    let target = draft_path(root, &source);
    let staging = root.join(format!("draft-{}.partial", Uuid::new_v4()));
    let publish = (|| -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&staging)
            .map_err(|e| e.to_string())?;
        file.write_all(&bytes)
            .and_then(|()| file.sync_all())
            .map_err(|e| e.to_string())?;
        drop(file);
        fs::rename(&staging, &target).map_err(|e| e.to_string())
    })();
    if let Err(reason) = publish {
        let _ = fs::remove_file(&staging);
        return Err(format!("草稿写入失败，原草稿保留：{reason}"));
    }
    Ok(SavedDraft {
        format_version: 1,
        draft_sha256: sha(&bytes),
        updated_at: draft.updated_at,
    })
}

fn delete(root: &Path, result: &Path, expected: Option<&str>) -> Result<(), String> {
    let source = source_identity(result)?;
    let _lock = lock(root, &source)?;
    let physical = physical_root(root, &source)?;
    let root = physical.as_path();
    check_expected(root, &source, expected)?;
    match fs::remove_file(draft_path(root, &source)) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn root(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_local_data_dir()
        .map(|p| p.join("edit-drafts-v1"))
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn read_edit_session(app: AppHandle, result_dir: PathBuf) -> Result<EditSession, String> {
    let root = root(&app)?;
    tauri::async_runtime::spawn_blocking(move || load(&root, &result_dir))
        .await
        .map_err(|e| e.to_string())?
}
#[tauri::command]
pub async fn save_edit_draft(
    app: AppHandle,
    draft: EditDraft,
    expected_sha256: Option<String>,
) -> Result<SavedDraft, String> {
    let root = root(&app)?;
    tauri::async_runtime::spawn_blocking(move || save(&root, draft, expected_sha256.as_deref()))
        .await
        .map_err(|e| e.to_string())?
}
#[tauri::command]
pub async fn delete_edit_draft(
    app: AppHandle,
    source_result: PathBuf,
    expected_sha256: Option<String>,
) -> Result<(), String> {
    let root = root(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        delete(&root, &source_result, expected_sha256.as_deref())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> EditDraft {
        serde_json::from_str(include_str!(
            "../../../../tests/fixtures/edit-draft-v1.json"
        ))
        .unwrap()
    }
    struct Workspace {
        root: PathBuf,
        source: PathBuf,
        drafts: PathBuf,
    }
    impl Workspace {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("glt-draft-test-{}", Uuid::new_v4()));
            let source = root.join("result");
            fs::create_dir_all(&source).unwrap();
            fs::write(
                source.join("performance.json"),
                serde_json::to_vec(&fixture().performance).unwrap(),
            )
            .unwrap();
            Self {
                drafts: root.join("app-data"),
                root,
                source,
            }
        }
        fn draft(&self) -> EditDraft {
            let session = load(&self.drafts, &self.source).unwrap();
            let mut draft = fixture();
            draft.source_result = session.source_result;
            draft.base_sha256 = session.base_sha256.unwrap();
            draft
        }
    }
    impl Drop for Workspace {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn shared_fixture_and_performance_are_validated() {
        validate(&fixture()).unwrap();
        let mut draft = fixture();
        draft.format_version = 2;
        assert!(validate(&draft).is_err());
        draft = fixture();
        draft.performance["notes"][0]["velocity"] = 200.into();
        assert!(validate(&draft).is_err());
        draft = fixture();
        draft.editor_state.tool = "unknown".into();
        assert!(validate(&draft).is_err());
        draft = fixture();
        draft.base_revision_id = "other".into();
        assert!(validate(&draft).is_err());
    }
    #[test]
    fn save_reload_compare_and_delete_never_modify_published_result() {
        let w = Workspace::new();
        let original = fs::read(w.source.join("performance.json")).unwrap();
        let mut draft = w.draft();
        draft.performance["notes"][0]["velocity"] = 45.into();
        let first = save(&w.drafts, draft.clone(), None).unwrap();
        let restored = load(&w.drafts, &w.source).unwrap();
        assert!(restored.can_restore);
        assert_eq!(
            restored.draft.unwrap().performance["notes"][0]["velocity"],
            45
        );
        draft.performance["notes"][0]["velocity"] = 70.into();
        let second = save(&w.drafts, draft, Some(&first.draft_sha256)).unwrap();
        assert_ne!(first.draft_sha256, second.draft_sha256);
        assert!(delete(&w.drafts, &w.source, Some(&first.draft_sha256)).is_err());
        delete(&w.drafts, &w.source, Some(&second.draft_sha256)).unwrap();
        assert!(load(&w.drafts, &w.source).unwrap().draft.is_none());
        assert_eq!(
            fs::read(w.source.join("performance.json")).unwrap(),
            original
        );
        assert_eq!(fs::read_dir(&w.source).unwrap().count(), 1);
    }
    #[test]
    fn changed_or_missing_baseline_preserves_draft_and_refuses_restore_and_write() {
        let w = Workspace::new();
        let draft = w.draft();
        let saved = save(&w.drafts, draft.clone(), None).unwrap();
        fs::write(w.source.join("performance.json"), b"{}").unwrap();
        let session = load(&w.drafts, &w.source).unwrap();
        assert!(!session.can_restore);
        assert!(session.draft.is_some());
        assert!(session.warning.is_some());
        assert!(save(&w.drafts, draft, Some(&saved.draft_sha256)).is_err());
        fs::remove_file(w.source.join("performance.json")).unwrap();
        let session = load(&w.drafts, &w.source).unwrap();
        assert!(!session.can_restore);
        assert!(session.draft.is_some());
        assert_eq!(session.draft_sha256.unwrap(), saved.draft_sha256);
    }
    #[test]
    fn concurrent_or_stale_writers_cannot_replace_a_draft() {
        let w = Workspace::new();
        let draft = w.draft();
        let saved = save(&w.drafts, draft.clone(), None).unwrap();
        assert!(save(&w.drafts, draft.clone(), None).is_err());
        let held = lock(&w.drafts, &draft.source_result).unwrap();
        assert!(save(&w.drafts, draft.clone(), Some(&saved.draft_sha256)).is_err());
        drop(held);
        assert_eq!(
            load(&w.drafts, &w.source).unwrap().draft_sha256.unwrap(),
            saved.draft_sha256
        );
    }
    #[test]
    fn corrupt_draft_is_retained_and_reported_without_hiding_valid_baseline() {
        let w = Workspace::new();
        let draft = w.draft();
        let path = draft_path(&w.drafts, &draft.source_result);
        fs::write(&path, b"{bad").unwrap();
        let session = load(&w.drafts, &w.source).unwrap();
        assert!(session.baseline.is_some());
        assert!(!session.can_restore);
        assert!(session.warning.is_some());
        assert_eq!(fs::read(&path).unwrap(), b"{bad");
    }
    #[cfg(windows)]
    #[test]
    fn failed_atomic_replacement_keeps_old_draft_and_removes_staging() {
        use std::os::windows::fs::OpenOptionsExt;
        let w = Workspace::new();
        let draft = w.draft();
        let first = save(&w.drafts, draft.clone(), None).unwrap();
        let target = draft_path(&w.drafts, &draft.source_result);
        let before = fs::read(&target).unwrap();
        let held = OpenOptions::new()
            .read(true)
            .share_mode(3)
            .open(&target)
            .unwrap();
        assert!(save(&w.drafts, draft, Some(&first.draft_sha256)).is_err());
        assert_eq!(fs::read(&target).unwrap(), before);
        assert!(!fs::read_dir(&w.drafts).unwrap().any(|entry| {
            entry
                .unwrap()
                .path()
                .extension()
                .is_some_and(|e| e == "partial")
        }));
        drop(held);
    }
}
