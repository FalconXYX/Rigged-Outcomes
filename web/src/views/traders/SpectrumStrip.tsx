import * as d3 from "d3";
import { useCallback, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import type { RubricDisplay, TradersData } from "../../types";
import { Tooltip } from "../../components/ui";
import { fmtInt, fmtUSDCompact } from "../../lib/format";
import { useSize } from "../../lib/useSize";
import { groupOf, type Focus, type Grouping, type Pin } from "./model";
import { font, textWidth, useFontVersion } from "./paint";
import { bandColor, layoutSpectrum, scoreX } from "./spectrum";
import { WalletTipBody } from "./TraderCard";

/**
 * The insider-score spectrum: every scored whale on one 0–100 axis (a
 * beeswarm, area ∝ position, colour = band), the bands as a rail under it,
 * each band named with its whale count and stake. Hover and keyboard focus
 * light the wallet in the other two charts; a click pins its card on the
 * constellation.
 */

const HIT = 18;
const R_MIN = 2.5;
const R_MAX = 11;
const RAIL = 6;
const NAME_FS = 12;
const META_FS = 11;
const TICK_FS = 11;
/** Below this radius a dot is too small for the top band's surface core (it would read as a ring). */
const CORE_MIN_R = 3.5;

interface Props {
  data: TradersData;
  rubric: RubricDisplay;
  grouping: Grouping;
  hidden: ReadonlySet<string>;
  focus: Focus;
  onFocus: (f: Focus) => void;
  onLeave: () => void;
  pin: Pin | null;
  onPin: (p: Pin | null) => void;
  restRows: Map<string, Set<number>>;
  walletRows: Map<string, number[]>;
}

/** Greedy word wrap into lines no wider than maxW; null when a single word is wider. */
function wrap(text: string, f: string, maxW: number): string[] | null {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let cur = "";
  for (const w of words) {
    const next = cur ? `${cur} ${w}` : w;
    if (textWidth(next, f) <= maxW) cur = next;
    else {
      if (!cur || textWidth(w, f) > maxW) return null;
      lines.push(cur);
      cur = w;
    }
  }
  if (cur) lines.push(cur);
  return lines;
}

export function SpectrumStrip({ data, rubric, grouping, hidden, focus, onFocus, onLeave, pin, onPin, restRows, walletRows }: Props) {
  const [wrapRef, { width: W }] = useSize<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const lastIdx = useRef(0);
  const kbIdx = useRef(-1);
  const [svgFocused, setSvgFocused] = useState(false);
  const fontVersion = useFontVersion();

  const narrow = W < 560;
  const swarmMax = narrow ? 150 : 180;
  const pad = R_MAX + 2;
  const [lo, hi] = rubric.scale;

  const swarm = useMemo(
    () => (W > 0 ? layoutSpectrum(rubric, data.traders, W, { rMin: R_MIN, rMax: R_MAX, pad, maxHeight: swarmMax }) : null),
    [rubric, data, W, pad, swarmMax],
  );

  /** Whale count and stake per band (hidden bands included: the axis always shows the whole scale). */
  const totals = useMemo(() => {
    const out = new Map<string, { count: number; stake: number }>(rubric.bands.map((b) => [b.id, { count: 0, stake: 0 }]));
    data.traders.forEach((tr, row) => {
      const slot = out.get(groupOf(grouping, row).id);
      if (!slot) return;
      slot.count += 1;
      slot.stake += tr.position;
    });
    return out;
  }, [rubric, data, grouping]);

  /**
   * Band labels under the rail: the name (wrapped to fit the band) over
   * "count · stake". One form for every band: when one name does not fit, no
   * band is named (the legend above names them), and the stake is dropped
   * before the count.
   */
  const bandLabels = useMemo(() => {
    void fontVersion;
    if (W <= 0) return { items: [], lines: 0 };
    const fName = font(600, NAME_FS);
    const fMeta = font(500, META_FS);
    const base = rubric.bands.map((b) => {
      const x0 = scoreX(rubric, b.min, W, pad);
      const x1 = scoreX(rubric, b.max, W, pad);
      const t = totals.get(b.id) ?? { count: 0, stake: 0 };
      return { band: b, x0, x1, cx: (x0 + x1) / 2, maxW: Math.max(0, x1 - x0 - 6), t };
    });
    const names = base.map((it) => wrap(it.band.label, fName, it.maxW));
    const showNames = names.every((n) => n !== null);
    const fullFits = base.every((it) => textWidth(`${fmtInt(it.t.count)} · ${fmtUSDCompact(it.t.stake)}`, fMeta) <= it.maxW);
    const countFits = base.every((it) => textWidth(fmtInt(it.t.count), fMeta) <= it.maxW);
    const items = base.map((it, i) => ({
      ...it,
      name: showNames ? names[i] : null,
      meta: fullFits ? `${fmtInt(it.t.count)} · ${fmtUSDCompact(it.t.stake)}` : countFits ? fmtInt(it.t.count) : null,
    }));
    const lines = showNames ? Math.max(0, ...names.map((n) => n?.length ?? 0)) : 0;
    return { items, lines };
  }, [rubric, totals, W, pad, fontVersion]);

  const top = 6;
  const swarmH = Math.ceil(Math.max(R_MAX * 2, swarm?.height ?? 0));
  const base = top + swarmH; // the baseline the dots rest on
  const railY = base + 4;
  const tickY = railY + RAIL + 13;
  const nameY = tickY + 18;
  const metaY = bandLabels.lines > 0 ? nameY + bandLabels.lines * (NAME_FS * 1.25) + 2 : tickY + 17;
  const H = Math.ceil(metaY + META_FS + 6);

  const dots = useMemo(
    () =>
      (swarm?.dots ?? []).map((d) => ({
        ...d,
        sy: base - d.y,
        g: groupOf(grouping, d.row),
      })),
    [swarm, base, grouping],
  );
  const visible = useMemo(() => dots.filter((d) => !hidden.has(d.g.id)), [dots, hidden]);
  const byRow = useMemo(() => new Map(dots.map((d) => [d.row, d])), [dots]);
  /** Keyboard order: left to right along the scale. */
  const kbOrder = useMemo(() => [...visible].sort((a, b) => a.x - b.x || a.sy - b.sy), [visible]);
  const delaunay = useMemo(() => d3.Delaunay.from(visible, (d) => d.x, (d) => d.sy), [visible]);

  const pick = useCallback(
    (clientX: number, clientY: number) => {
      const svg = svgRef.current;
      if (!svg || visible.length === 0) return null;
      const rect = svg.getBoundingClientRect();
      const px = clientX - rect.left;
      const py = clientY - rect.top;
      const i = delaunay.find(px, py, Math.min(lastIdx.current, visible.length - 1));
      lastIdx.current = i;
      const d = visible[i];
      if (!d) return null;
      return Math.hypot(d.x - px, d.sy - py) <= Math.max(HIT, d.r + 4) ? d : null;
    },
    [delaunay, visible],
  );

  /** The band under a point of the rail / label area (for band hover). */
  const bandAt = (px: number, py: number) => {
    if (py < base) return null;
    return bandLabels.items.find((it) => px >= it.x0 && px < it.x1) ?? null;
  };

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "touch") return;
    const d = pick(e.clientX, e.clientY);
    if (d) return void onFocus({ kind: "wallet", row: d.row, source: "spectrum" });
    const rect = svgRef.current!.getBoundingClientRect();
    const b = bandAt(e.clientX - rect.left, e.clientY - rect.top);
    if (b && !hidden.has(b.band.id)) onFocus({ kind: "category", category: b.band.id, source: "spectrum" });
    else onLeave();
  };

  const onClick = (e: MouseEvent<SVGSVGElement>) => {
    const d = pick(e.clientX, e.clientY);
    if (d) onPin(pin?.row === d.row ? null : { row: d.row, source: "constellation" });
    else onPin(null);
  };

  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!kbOrder.length) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1;
      kbIdx.current = (kbIdx.current + step + kbOrder.length) % kbOrder.length;
      onFocus({ kind: "wallet", row: kbOrder[kbIdx.current].row, source: "spectrum" });
    } else if ((e.key === "Enter" || e.key === " ") && focus?.kind === "wallet" && focus.source === "spectrum") {
      e.preventDefault();
      onPin({ row: focus.row, source: "constellation" });
    }
  };

  /* ── Highlight ───────────────────────────────────────────────────── */

  const isOn = useMemo<((d: { row: number; g: { id: string } }) => boolean) | null>(() => {
    if (!focus) return null;
    if (focus.kind === "wallet") {
      const row = focus.row;
      return (d) => d.row === row;
    }
    const cat = focus.category;
    if (focus.kind === "category") return (d) => d.g.id === cat;
    const rows = restRows.get(cat);
    return (d) => d.g.id === cat && !!rows?.has(d.row);
  }, [focus, restRows]);
  const litBand = focus && focus.kind !== "wallet" ? focus.category : focus?.kind === "wallet" ? byRow.get(focus.row)?.g.id : undefined;

  const focusDot = focus?.kind === "wallet" ? byRow.get(focus.row) : undefined;
  const pinDot = pin ? byRow.get(pin.row) : undefined;
  const showTip = !!focusDot && focus?.source === "spectrum" && !hidden.has(focusDot.g.id);

  const cuts = useMemo(() => {
    const s = new Set<number>([lo, hi]);
    for (const b of rubric.bands) s.add(b.min);
    return [...s].filter((v) => v >= lo && v <= hi).sort((a, b) => a - b);
  }, [rubric, lo, hi]);

  return (
    <div className="tr-spectrum">
      <div ref={wrapRef} className="chart tr-chart" style={{ height: W > 0 ? H : undefined }}>
        {W > 0 && swarm && (
          <>
            <svg
              ref={svgRef}
              width={W}
              height={H}
              className="tr-svg tr-sp"
              role="img"
              aria-label={`${rubric.name} of ${data.traders.length} whale wallets, ${lo} to ${hi}. Arrow keys step through wallets, Enter pins one.`}
              tabIndex={0}
              style={{ cursor: focusDot && focus?.source === "spectrum" ? "pointer" : "default", touchAction: "pan-y" }}
              onPointerMove={onPointerMove}
              onPointerLeave={onLeave}
              onClick={onClick}
              onKeyDown={onKeyDown}
              onFocus={(e) => setSvgFocused(e.currentTarget.matches(":focus-visible"))}
              onBlur={() => {
                setSvgFocused(false);
                if (focus?.source === "spectrum") onLeave();
              }}
            >
              <rect width={W} height={H} className="tr-bg" />

              {/* Band cut-offs: a hairline from the rail up through the swarm. */}
              <g className="sp-cuts">
                {cuts.slice(1, -1).map((c) => {
                  const x = scoreX(rubric, c, W, pad);
                  return <line key={c} x1={x} x2={x} y1={top} y2={railY + RAIL} />;
                })}
              </g>

              <g className="sp-dots">
                {dots.map((d) => {
                  const off = hidden.has(d.g.id);
                  const dim = !off && isOn && !isOn(d);
                  return (
                    <g key={d.row} className={off ? "off" : dim ? "dim" : undefined} style={{ fill: d.g.color }}>
                      <circle className="dot" cx={d.x} cy={d.sy} r={d.r} />
                      {d.g.core && d.r >= CORE_MIN_R && <circle className="dot-core" cx={d.x} cy={d.sy} r={d.r * 0.32} />}
                    </g>
                  );
                })}
              </g>

              {/* The bands as one rail, 2px surface gaps at the cut-offs. */}
              <g className="sp-rail">
                {bandLabels.items.map((it) => (
                  <rect
                    key={it.band.id}
                    x={it.x0 + (it.band.min > lo ? 1 : 0)}
                    y={railY}
                    width={Math.max(0, it.x1 - it.x0 - (it.band.min > lo ? 1 : 0) - (it.band.max < hi ? 1 : 0))}
                    height={RAIL}
                    className={hidden.has(it.band.id) ? "off" : litBand && litBand !== it.band.id ? "dim" : undefined}
                    style={{ fill: bandColor(it.band) }}
                  />
                ))}
              </g>

              <g className="sp-ticks">
                {cuts.map((c, i) => (
                  <text
                    key={c}
                    x={scoreX(rubric, c, W, pad)}
                    y={tickY}
                    textAnchor={i === 0 ? "start" : i === cuts.length - 1 ? "end" : "middle"}
                    dx={i === 0 ? -pad + 2 : i === cuts.length - 1 ? pad - 2 : 0}
                    style={{ fontSize: TICK_FS }}
                  >
                    {c}
                  </text>
                ))}
              </g>

              <g className="sp-bands">
                {bandLabels.items.map((it) => (
                  <g key={it.band.id} className={hidden.has(it.band.id) ? "off" : litBand && litBand !== it.band.id ? "dim" : undefined}>
                    {it.name && (
                      <text className="sp-name" x={it.cx} y={nameY} textAnchor="middle" style={{ fontSize: NAME_FS }}>
                        {it.name.map((l, i) => (
                          <tspan key={i} x={it.cx} dy={i === 0 ? 0 : NAME_FS * 1.25}>
                            {l}
                          </tspan>
                        ))}
                      </text>
                    )}
                    {it.meta && (
                      <text className="sp-meta" x={it.cx} y={metaY} textAnchor="middle" style={{ fontSize: META_FS }}>
                        {it.meta}
                      </text>
                    )}
                  </g>
                ))}
              </g>

              {/* Hover + pin overlays */}
              {[pinDot, focusDot !== pinDot ? focusDot : undefined].map((d) => {
                if (!d || hidden.has(d.g.id)) return null;
                const big = d.r * 1.15 + 1.5;
                return (
                  <g key={`ov-${d.row}`} className="sp-overlay" style={{ fill: d.g.color }}>
                    <circle className={`focus-ring${d === pinDot ? " pinned" : ""}`} cx={d.x} cy={d.sy} r={big + 3} />
                    <circle className="dot" cx={d.x} cy={d.sy} r={big} />
                    {d.g.core && big >= CORE_MIN_R && <circle className="dot-core" cx={d.x} cy={d.sy} r={big * 0.32} />}
                  </g>
                );
              })}
            </svg>

            {showTip && focusDot && (
              <Tooltip x={focusDot.x} y={focusDot.sy} visible containerWidth={W} containerHeight={H}>
                <WalletTipBody
                  trader={data.traders[focusDot.row]}
                  group={focusDot.g}
                  rubric={rubric}
                  siblings={(walletRows.get(data.traders[focusDot.row].id)?.length ?? 1) - 1}
                  footer={pin?.row === focusDot.row ? "Pinned · click again to unpin" : svgFocused ? "Enter to pin details" : "Click to pin details"}
                />
              </Tooltip>
            )}
          </>
        )}
      </div>
    </div>
  );
}
