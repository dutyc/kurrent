import './Select.css'

export default function Select({ label, value, onChange, options, required, disabled, name, placeholder, style }) {
  return (
    <div className="field" style={style}>
      {label && (
        <label className="field-label" htmlFor={name}>
          {label}
          {required && <span className="field-required"> *</span>}
        </label>
      )}
      <div className="select-wrap">
        <select
          id={name}
          name={name}
          className="field-input field-select"
          value={value}
          onChange={onChange}
          required={required}
          disabled={disabled}
        >
          {placeholder && (
            <option value="" disabled>
              {placeholder}
            </option>
          )}
          {options.map((opt) => (
            <option key={opt.value} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))}
        </select>
        <span className="select-arrow">&#9660;</span>
      </div>
    </div>
  )
}
