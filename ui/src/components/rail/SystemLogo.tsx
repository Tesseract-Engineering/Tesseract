import { useEffect, useState } from "react";
import { logoSrc, type SystemInfo } from "@/lib/systems";

const LOAD_TIMEOUT_MS = 3000;

/** Brand logo from Simple Icons, a code glyph for code, a monogram if the icon fails or stalls. */
export function SystemLogo({ system, size = 16, ink }: { system: SystemInfo; size?: number; ink?: string }) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const src = logoSrc(system, ink);
  useEffect(() => {
    if (!src || loaded) return;
    const t = setTimeout(() => setFailed(true), LOAD_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [src, loaded]);
  if (system.isCode) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill="none"
        stroke="#37352F"
        strokeWidth={1.3}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="flex-none"
        aria-hidden="true"
      >
        <path d="M5 4 L1.5 8 L5 12 M11 4 L14.5 8 L11 12 M9.5 2.5 L6.5 13.5" />
      </svg>
    );
  }
  if (!src || failed) {
    return (
      <span
        className="inline-flex flex-none items-center justify-center rounded-[3px] font-semibold leading-none"
        style={{
          width: size,
          height: size,
          fontSize: system.mono.length > 1 ? size * 0.5 : size * 0.62,
          background: ink ? "#E9E9E7" : system.color,
          color: ink ? "#37352F" : "#fff",
        }}
        aria-hidden="true"
      >
        {system.mono}
      </span>
    );
  }
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className="block flex-none"
      style={{ width: size, height: size }}
      onLoad={() => setLoaded(true)}
      onError={() => setFailed(true)}
    />
  );
}
