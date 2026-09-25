interface Props {
  label: string;
  minimum: number;
  maximum: number;
  step: number;
  value: number | null;
  fallback: number;
  unit?: string;
  precision?: number;
  unsetLabel?: string;
  onChange: (value: number | null) => void;
}

function display(value: number, precision: number, unit: string): string {
  const formatted = precision > 0 ? value.toFixed(precision) : String(Math.round(value));
  return unit ? `${formatted} ${unit}` : formatted;
}

export default function ParameterSlider({
  label,
  minimum,
  maximum,
  step,
  value,
  fallback,
  unit = "",
  precision = 0,
  unsetLabel,
  onChange,
}: Props) {
  const current = Math.min(maximum, Math.max(minimum, value ?? fallback));
  const percent = ((current - minimum) / Math.max(step, maximum - minimum)) * 100;
  return (
    <div className="parameter-slider">
      <div className="parameter-slider-heading">
        <span>{label}</span>
        <strong>{value === null && unsetLabel ? unsetLabel : display(current, precision, unit)}</strong>
      </div>
      <div className="parameter-slider-row">
        <input
          type="range"
          min={minimum}
          max={maximum}
          step={step}
          value={current}
          onChange={(event) => onChange(Number(event.target.value))}
          style={{
            background: `linear-gradient(90deg, #d47c20 0 ${percent}%, #191e21 ${percent}% 100%)`,
          }}
        />
        <input
          className="parameter-number"
          type="number"
          min={minimum}
          max={maximum}
          step={step}
          value={value ?? ""}
          placeholder={unsetLabel ?? display(fallback, precision, unit)}
          onChange={(event) => {
            const raw = event.target.value.trim();
            onChange(raw ? Number(raw) : null);
          }}
        />
        {value !== null && unsetLabel && (
          <button type="button" className="parameter-reset" onClick={() => onChange(null)}>
            自动
          </button>
        )}
      </div>
    </div>
  );
}
