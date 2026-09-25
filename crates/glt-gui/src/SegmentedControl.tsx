interface Option<T extends string> {
  value: T;
  label: string;
  hint?: string;
}

interface Props<T extends string> {
  label: string;
  value: T;
  options: readonly Option<T>[];
  onChange: (value: T) => void;
}

export default function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: Props<T>) {
  return (
    <div className="segmented-field">
      <span>{label}</span>
      <div className="segmented-control" role="radiogroup" aria-label={label}>
        {options.map((option) => (
          <button
            type="button"
            role="radio"
            aria-checked={option.value === value}
            className={option.value === value ? "active" : ""}
            key={option.value}
            title={option.hint}
            onClick={() => onChange(option.value)}
          >
            <strong>{option.label}</strong>
            {option.hint && <small>{option.hint}</small>}
          </button>
        ))}
      </div>
    </div>
  );
}
