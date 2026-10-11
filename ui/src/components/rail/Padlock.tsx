export function Padlock({ size = 12, color = "#787774" }: { size?: number; color?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke={color}
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="flex-none"
      aria-hidden="true"
    >
      <path d="M4 7 H12 V14 H4 Z M5.5 7 V5 a2.5 2.5 0 0 1 5 0 V7" />
    </svg>
  );
}
