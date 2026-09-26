import { useState } from "react";

import {
  DEFAULT_MAPPING_PROFILE,
  defaultMappingKeys,
  pitchName,
  shiftMappingKey,
  swapMappingKeys,
  validateMappingKeys,
} from "./portableConfig";
import type { MappingKey } from "./types";

interface Props {
  profile: string;
  keys: MappingKey[];
  onChange: (profile: string, keys: MappingKey[]) => void;
}

const ROW_SIZE = 7;

export default function MappingEditor({ profile, keys, onChange }: Props) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rows = [keys.slice(0, ROW_SIZE), keys.slice(ROW_SIZE, ROW_SIZE * 2), keys.slice(ROW_SIZE * 2)];

  function commit(next: MappingKey[]) {
    try {
      const validated = validateMappingKeys(next);
      setError(null);
      onChange(profile || DEFAULT_MAPPING_PROFILE, validated);
    } catch (reason) {
      setError(String(reason));
    }
  }

  function selectOrSwap(key: string) {
    if (!selectedKey) {
      setSelectedKey(key);
      setError(null);
      return;
    }
    if (selectedKey === key) {
      setSelectedKey(null);
      return;
    }
    commit(swapMappingKeys(keys, selectedKey, key));
    setSelectedKey(key);
  }

  return (
    <section className="mapping-editor">
      <header className="mapping-editor-header">
        <div>
          <span>MAPPING LAYOUT</span>
          <strong>21 键映射编辑器</strong>
          <small>先点一个键，再点另一个键即可交换音高；按钮支持 ±1 / ±12 移调。</small>
        </div>
        <label>
          <span>配置名称</span>
          <input
            value={profile}
            onChange={(event) => onChange(event.target.value, keys)}
            placeholder={DEFAULT_MAPPING_PROFILE}
          />
        </label>
      </header>

      <div className="mapping-keyboard">
        {rows.map((row, rowIndex) => (
          <div className="mapping-key-row" key={rowIndex}>
            {row.map((entry) => (
              <button
                type="button"
                key={entry.key}
                className={selectedKey === entry.key ? "selected" : ""}
                aria-pressed={selectedKey === entry.key}
                onClick={() => selectOrSwap(entry.key)}
              >
                <strong>{entry.key}</strong>
                <span>{pitchName(entry.pitch)}</span>
                <small>MIDI {entry.pitch}</small>
              </button>
            ))}
          </div>
        ))}
      </div>

      <div className="mapping-editor-actions">
        <span>
          {selectedKey ? `当前键：${selectedKey} · ${pitchName(keys.find((entry) => entry.key === selectedKey)?.pitch ?? 48)}` : "未选择琴键"}
        </span>
        {[-12, -1, 1, 12].map((delta) => (
          <button
            type="button"
            key={delta}
            disabled={!selectedKey}
            onClick={() => selectedKey && commit(shiftMappingKey(keys, selectedKey, delta))}
          >
            {delta > 0 ? `+${delta}` : delta}
          </button>
        ))}
        <button
          type="button"
          onClick={() => {
            onChange(DEFAULT_MAPPING_PROFILE, defaultMappingKeys());
            setSelectedKey(null);
            setError(null);
          }}
        >
          重置 C 调
        </button>
        {error && <em>{error}</em>}
      </div>
    </section>
  );
}
