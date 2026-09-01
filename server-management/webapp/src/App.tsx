import type { FormEvent } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AggregatePayload, Container, Device, Disk, Machine, ServersPayload } from './types';
import { bytes, clampPercent, isIdleDevice, mb, number, percent, precise, timeAgo } from './format';

type View = 'overview' | 'server' | 'history' | 'servers';
type MetricKey = 'npu_util_percent' | 'hbm_percent' | 'cpu_percent' | 'memory_percent' | 'disk_max_percent';

function ServerTags({ tags, compact = false }: { tags: string[]; compact?: boolean }) {
  if (!tags.length) return null;
  return (
    <span className={`server-tags ${compact ? 'server-tags--compact' : ''}`}>
      {tags.map((tag) => (
        <span key={tag}>{tag}</span>
      ))}
    </span>
  );
}

const REFRESH_OPTIONS = [5, 10, 30];
const metricLabels: Record<MetricKey, string> = {
  npu_util_percent: 'NPU 利用率',
  hbm_percent: 'HBM 占用',
  cpu_percent: 'CPU 利用率',
  memory_percent: '系统内存',
  disk_max_percent: '磁盘水位',
};

/* ---------------- 小组件 ---------------- */

function StatusDot({ status }: { status: 'online' | 'offline' }) {
  return <span className={`status-dot status-dot--${status}`} aria-hidden="true" />;
}

function dieColor(hbmPercent: number | null) {
  const ratio = Math.min(1, clampPercent(hbmPercent) / 50);
  const start = [235, 242, 252];
  const end = [32, 111, 235];
  const channel = (index: number) => Math.round(start[index] + (end[index] - start[index]) * ratio);
  return `rgb(${channel(0)} ${channel(1)} ${channel(2)})`;
}

function NpuDie({ device, chip, detail = false }: { device: Device; chip?: Device['chips'][number]; detail?: boolean }) {
  const telemetry = chip ?? device;
  const hbm = percent(telemetry.mem_used_mb, telemetry.mem_total_mb);
  const active = clampPercent(telemetry.aicore_util) > 0;
  const identity = chip ? `NPU ${device.id} / ${chip.bus_id}` : `NPU ${device.id}`;
  return (
    <span
      className={`npu-die ${detail ? 'npu-die--detail' : ''} ${hbm != null && hbm >= 32 ? 'is-dark' : ''}`}
      style={{ backgroundColor: dieColor(hbm) }}
      title={`${identity} · HBM ${number(hbm, '%')} · AICore ${number(telemetry.aicore_util, '%')}`}
    >
      <b>{device.id}</b>
      {detail && <small>HBM {number(hbm, '%')}</small>}
      {active && <i className="npu-die__activity" aria-label="AICore 活跃" />}
    </span>
  );
}

type DieEntry = { device: Device; chip?: Device['chips'][number] };

function physicalDies(devices: Device[]): DieEntry[] {
  return devices.flatMap((device): DieEntry[] =>
    device.chips.length ? device.chips.map((chip) => ({ device, chip })) : [{ device, chip: undefined }],
  );
}

function MiniBar({ value, tone = 'blue' }: { value?: number | null; tone?: 'blue' | 'green' | 'orange' | 'purple' }) {
  return (
    <span className={`mini-bar mini-bar--${tone}`}>
      <i style={{ width: `${clampPercent(value)}%` }} />
    </span>
  );
}

function MetricCard({
  label, value, suffix, detail, tone = 'blue', icon,
}: { label: string; value: string; suffix?: string; detail: string; tone?: string; icon: string }) {
  return (
    <article className={`metric-card metric-card--${tone}`}>
      <div className="metric-card__head">
        <span>{label}</span>
        <i aria-hidden="true">{icon}</i>
      </div>
      <strong>
        {value}
        <small>{suffix}</small>
      </strong>
      <p>{detail}</p>
    </article>
  );
}

/* ---------------- 趋势图（canvas 手绘渐变面积图，对齐 vaws-top） ---------------- */

type TrendPoint = { bucket: number } & Record<string, number | null | undefined>;

function TrendCanvas({ points, metric }: { points: TrendPoint[]; metric: MetricKey }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      const box = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, box.width * ratio);
      canvas.height = Math.max(1, box.height * ratio);
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, box.width, box.height);
      ctx.font = '11px Inter, system-ui, sans-serif';
      ctx.fillStyle = '#84909f';
      ctx.textAlign = 'right';
      for (let index = 0; index <= 4; index += 1) {
        const y = 18 + (index * (box.height - 42)) / 4;
        ctx.strokeStyle = '#e7ebf0';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(42, y);
        ctx.lineTo(box.width - 8, y);
        ctx.stroke();
        ctx.fillText(`${100 - index * 25}%`, 34, y + 4);
      }
      const usable = points.filter((point) => point[metric] != null);
      if (usable.length < 2) {
        ctx.textAlign = 'center';
        ctx.fillText('当前窗口暂无足够历史样本', box.width / 2, box.height / 2);
        return;
      }
      const coordinates = usable.map((point, index) => [
        42 + (index * (box.width - 52)) / (usable.length - 1),
        18 + ((100 - clampPercent(point[metric] ?? 0)) * (box.height - 42)) / 100,
      ]);
      const gradient = ctx.createLinearGradient(0, 12, 0, box.height);
      gradient.addColorStop(0, 'rgba(32, 111, 235, .26)');
      gradient.addColorStop(1, 'rgba(32, 111, 235, 0)');
      ctx.beginPath();
      ctx.moveTo(coordinates[0][0], box.height - 24);
      coordinates.forEach(([x, y]) => ctx.lineTo(x, y));
      ctx.lineTo(coordinates[coordinates.length - 1][0], box.height - 24);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
      ctx.beginPath();
      coordinates.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.strokeStyle = '#206feb';
      ctx.lineWidth = 2.5;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [points, metric]);
  return <canvas className="trend-canvas" ref={ref} aria-label={`${metricLabels[metric]}历史趋势`} />;
}

/* ---------------- 热力图（2 小时粒度） ---------------- */

function dateKey(timestamp: number) {
  const date = new Date(timestamp * 1000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function buildDays(dayCount: number) {
  const end = new Date();
  end.setHours(0, 0, 0, 0);
  return Array.from({ length: dayCount }, (_, index) => {
    const date = new Date(end);
    date.setDate(end.getDate() - (dayCount - index - 1));
    return {
      key: dateKey(date.getTime() / 1000),
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      full: `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`,
    };
  });
}

function heatLevel(value: number | null) {
  if (value == null) return 'empty';
  if (value < 3) return '0';
  return String(Math.min(5, Math.ceil(clampPercent(value) / 20)));
}

function HistoryHeatmap({
  title, subtitle, points, days, tone, valueFor,
}: {
  title: string;
  subtitle: string;
  points: TrendPoint[];
  days: ReturnType<typeof buildDays>;
  tone: 'blue' | 'green' | 'purple' | 'orange';
  valueFor: (point: TrendPoint) => number | null;
}) {
  const index = useMemo(
    () =>
      new Map(
        points.map((point) => {
          const date = new Date(point.bucket * 1000);
          return [`${dateKey(point.bucket)}:${Math.floor(date.getHours() / 2)}`, point] as const;
        }),
      ),
    [points],
  );
  const cellSize = days.length > 45 ? 9 : days.length > 14 ? 11 : 13;
  return (
    <article className={`heatmap-card heatmap-card--${tone}`}>
      <header>
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        <span>2h AVG</span>
      </header>
      <div className="heatmap-scroll">
        <div className="heatmap-grid" style={{ gridTemplateColumns: `26px repeat(${days.length}, ${cellSize}px)` }}>
          <i />
          {days.map((day, dayIndex) => (
            <b key={day.key} title={day.full}>
              {dayIndex % (days.length > 35 ? 7 : days.length > 14 ? 4 : 1) === 0 || dayIndex === days.length - 1
                ? day.label
                : ''}
            </b>
          ))}
          {Array.from({ length: 12 }, (_, row) => [
            <em key={`time-${row}`}>{String(row * 2).padStart(2, '0')}</em>,
            ...days.map((day) => {
              const point = index.get(`${day.key}:${row}`);
              const value = point ? valueFor(point) : null;
              return (
                <span
                  key={`${day.key}-${row}`}
                  className={`heat-cell heat-cell--${heatLevel(value)}`}
                  title={`${day.full} ${String(row * 2).padStart(2, '0')}:00 · ${value == null ? '无样本' : `${value.toFixed(1)}%`}`}
                />
              );
            }),
          ])}
        </div>
      </div>
      <footer>
        <span>低</span>
        {Array.from({ length: 6 }, (_, level) => (
          <i className={`heat-cell--${level}`} key={level} />
        ))}
        <span>高</span>
      </footer>
    </article>
  );
}

/* ---------------- 服务器卡片（总览） ---------------- */

function ServerCard({
  machine, onOpen, onCollect, collecting,
}: { machine: Machine; onOpen: () => void; onCollect: () => void; collecting: boolean }) {
  const devices = machine.npu?.npus ?? [];
  const dies = physicalDies(devices);
  const hbmUsed = devices.reduce((sum, d) => sum + (d.mem_used_mb ?? 0), 0);
  const hbmTotal = devices.reduce((sum, d) => sum + (d.mem_total_mb ?? 0), 0);
  const hbm = percent(hbmUsed, hbmTotal);
  const activeDies = dies.filter(({ device, chip }) => clampPercent((chip ?? device).aicore_util) > 0).length;
  return (
    <article className={`server-card server-card--${machine.reachable ? 'online' : 'offline'}`}>
      <button className="server-card__open" onClick={onOpen} aria-label={`查看 ${machine.alias} 详情`}>
        <header>
          <div className="server-card__identity">
            <StatusDot status={machine.reachable ? 'online' : 'offline'} />
            <div>
              <h3>{machine.alias}</h3>
              <p>{machine.host}</p>
            </div>
          </div>
          <span className="chevron">›</span>
        </header>
        <div
          className="device-strip"
          style={dies.length ? { gridTemplateColumns: `repeat(${dies.length}, minmax(0, 1fr))` } : undefined}
          aria-label={`${devices.length} 张 NPU，${dies.length} 个 die`}
        >
          {dies.length
            ? dies.map(({ device, chip }) => (
                <NpuDie key={`${device.id}-${chip?.bus_id ?? 'logical'}`} device={device} chip={chip} />
              ))
            : <p>{machine.error || '等待首次设备采样'}</p>}
        </div>
        <div className="server-card__metrics">
          <div>
            <span>CPU</span>
            <strong>{number(machine.cpu_percent, '%')}</strong>
            <MiniBar value={machine.cpu_percent} />
          </div>
          <div>
            <span>内存</span>
            <strong>{number(percent(machine.memory_total_bytes! - machine.memory_available_bytes!, machine.memory_total_bytes), '%')}</strong>
            <MiniBar value={percent(machine.memory_total_bytes! - machine.memory_available_bytes!, machine.memory_total_bytes)} tone="purple" />
          </div>
          <div>
            <span>HBM</span>
            <strong>{number(hbm, '%')}</strong>
            <MiniBar value={hbm} tone="green" />
          </div>
        </div>
      </button>
      <footer>
        <span>
          {activeDies} die 活跃 · {dies.length - activeDies} die 可用 · {timeAgo(machine.probed_at)}
          {machine.load1 != null ? ` · load1 ${precise(machine.load1)}` : ''}
        </span>
        <button onClick={onCollect} disabled={collecting}>
          {collecting ? '采集中…' : '立即采集'}
        </button>
      </footer>
    </article>
  );
}

/* ---------------- NPU 进程归属面板 ---------------- */

function NpuProcessPanel({ devices }: { devices: Device[] }) {
  const processes = Array.from(
    devices.reduce((byPid, device) => {
      for (const process of device.processes ?? []) {
        const current = byPid.get(process.pid) ?? { ...process, npuIds: [] as number[] };
        if (!current.npuIds.includes(device.id)) current.npuIds.push(device.id);
        byPid.set(process.pid, current);
      }
      return byPid;
    }, new Map<number, (Device['processes'][number] & { npuIds: number[] })>()),
  ).map(([, value]) => value);
  if (!processes.length) return null;
  return (
    <section className="panel process-panel">
      <header className="panel-heading">
        <div>
          <p className="section-kicker">NPU workloads</p>
          <h2>NPU 进程归属</h2>
          <span>按 PID 聚合自 npu-smi 进程表</span>
        </div>
        <span className="status-label status-label--busy">{processes.length} 个进程</span>
      </header>
      <div className="process-groups">
        {processes.map((process) => (
          <details className="process-group" key={process.pid}>
            <summary>
              <span className="process-container-icon is-container">▣</span>
              <span>
                <strong className="process-container-name">{process.name}</strong>
                <small>
                  PID {process.pid} · NPU {process.npuIds.join(', ')} · {mb(process.memory_mb)}
                </small>
              </span>
              <span className="process-summary-actions">
                <i>›</i>
              </span>
            </summary>
            <div className="process-details">
              <article>
                <header>
                  <strong>{process.name}</strong>
                  <span>PID {process.pid}</span>
                </header>
                <dl>
                  <div>
                    <dt>显存占用</dt>
                    <dd>
                      <code>{mb(process.memory_mb)}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>占用 NPU</dt>
                    <dd>
                      <code>NPU {process.npuIds.join(', ')}</code>
                    </dd>
                  </div>
                </dl>
              </article>
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}

/* ---------------- 服务器详情 ---------------- */

function ServerDetail({
  machine, onHistory, onCollect, collecting,
}: { machine: Machine; onHistory: () => void; onCollect: () => void; collecting: boolean }) {
  const devices = machine.npu?.npus ?? [];
  const dies = physicalDies(devices);
  const disks: Disk[] = machine.disks ?? [];
  const containers: Container[] = machine.docker ?? [];
  const memoryPercent = percent(
    machine.memory_total_bytes! - machine.memory_available_bytes!,
    machine.memory_total_bytes,
  );
  const hbmUsed = devices.reduce((sum, d) => sum + (d.mem_used_mb ?? 0), 0);
  const hbmTotal = devices.reduce((sum, d) => sum + (d.mem_total_mb ?? 0), 0);
  const hbmPercent = percent(hbmUsed, hbmTotal);
  const diskMax = disks.length ? Math.max(...disks.map((disk) => disk.use_pct ?? 0)) : null;
  const availableBytes = (disk: Disk) =>
    disk.total_gb != null && disk.used_gb != null ? (disk.total_gb - disk.used_gb) * 1024 ** 3 : null;

  return (
    <div className="page-stack server-detail">
      <section className="server-hero panel">
        <div className="server-hero__identity">
          <span className="server-glyph">S</span>
          <div>
            <span className={`status-label status-label--${machine.reachable ? 'online' : 'offline'}`}>
              <StatusDot status={machine.reachable ? 'online' : 'offline'} />
              {machine.reachable ? '在线' : '离线'}
            </span>
            <h2>{machine.alias}</h2>
            <p>
              {machine.user}@{machine.host}:{machine.port}
            </p>
            <div>
              <span>{devices.length} NPU</span>
              <span>采集于 {timeAgo(machine.probed_at)}</span>
            </div>
          </div>
        </div>
        <div className="server-hero__actions">
          <button className="secondary-button" onClick={onHistory}>
            查看历史
          </button>
          <button className="primary-button" onClick={onCollect} disabled={collecting}>
            {collecting ? '采集中…' : '立即采集'}
          </button>
        </div>
      </section>

      <section className="resource-metrics">
        <article>
          <header>
            <span>CPU 利用率</span>
            <strong>{number(machine.cpu_percent, '%')}</strong>
          </header>
          <MiniBar value={machine.cpu_percent} />
          <p>Load 1m {precise(machine.load1)}</p>
        </article>
        <article>
          <header>
            <span>系统内存</span>
            <strong>{number(memoryPercent, '%')}</strong>
          </header>
          <MiniBar value={memoryPercent} tone="purple" />
          <p>
            {bytes(machine.memory_total_bytes! - machine.memory_available_bytes!)} / {bytes(machine.memory_total_bytes)}
          </p>
        </article>
        <article>
          <header>
            <span>HBM 占用</span>
            <strong>{number(hbmPercent, '%')}</strong>
          </header>
          <MiniBar value={hbmPercent} tone="green" />
          <p>
            {mb(hbmUsed)} / {mb(hbmTotal)}
          </p>
        </article>
        <article>
          <header>
            <span>磁盘峰值</span>
            <strong>{number(diskMax, '%')}</strong>
          </header>
          <MiniBar value={diskMax} tone="orange" />
          <p>{disks.length} 个文件系统</p>
        </article>
      </section>

      <section className="panel device-panel">
        <header className="panel-heading">
          <div>
            <p className="section-kicker">Accelerators</p>
            <h2>NPU 设备</h2>
            <span>
              {devices.length} 张 NPU · {dies.length} 个 die ·{' '}
              {dies.filter(({ device, chip }) => clampPercent((chip ?? device).aicore_util) > 0).length} 个活跃 ·{' '}
              {timeAgo(machine.probed_at)}
            </span>
          </div>
        </header>
        {devices.length ? (
          <div className="device-grid">
            {devices.map((device) => {
              const deviceHbm = percent(device.mem_used_mb, device.mem_total_mb);
              return (
                <article className={`device-card ${clampPercent(device.aicore_util) > 0 ? 'is-busy' : ''}`} key={device.id}>
                  <header>
                    <span className="device-die-pair">
                      {device.chips.length
                        ? device.chips.map((chip) => <NpuDie key={chip.bus_id} device={device} chip={chip} />)
                        : <NpuDie device={device} detail />}
                    </span>
                    <div>
                      <h3>NPU {device.id}</h3>
                      <p>
                        {device.name || 'Ascend NPU'} · {device.health || '状态未知'}
                      </p>
                    </div>
                    <span className={`status-label ${clampPercent(device.aicore_util) > 0 ? 'status-label--busy' : 'status-label--idle'}`}>
                      {clampPercent(device.aicore_util) > 0 ? 'AICore 活跃' : '可用'}
                    </span>
                  </header>
                  <div className="device-stat">
                    <span>AICore</span>
                    <strong>{number(device.aicore_util, '%')}</strong>
                    <MiniBar value={device.aicore_util} tone="purple" />
                  </div>
                  <div className="device-stat">
                    <span>HBM</span>
                    <strong>{number(deviceHbm, '%')}</strong>
                    <MiniBar value={deviceHbm} tone="green" />
                  </div>
                  <footer>
                    <span>{number(device.temp_c, '°C')}</span>
                    <span>{number(device.power_w, 'W')}</span>
                    <span>{device.processes?.length || 0} 进程</span>
                  </footer>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="empty-state">
            <strong>暂无 NPU 数据</strong>
            <p>{machine.error || '等待下一次采集'}</p>
          </div>
        )}
      </section>

      <NpuProcessPanel devices={devices} />

      <section className="detail-columns">
        <article className="panel storage-panel">
          <header className="panel-heading">
            <div>
              <p className="section-kicker">Storage</p>
              <h2>磁盘与挂载</h2>
            </div>
          </header>
          <div className="storage-list">
            {disks.map((disk) => (
              <div key={`${disk.filesystem}-${disk.mount}`}>
                <header>
                  <span>
                    <strong>{disk.mount}</strong>
                    <small>{disk.filesystem}</small>
                  </span>
                  <b>{disk.use_pct}%</b>
                </header>
                <MiniBar value={disk.use_pct} tone={disk.use_pct >= 85 ? 'orange' : 'blue'} />
                <p>
                  {bytes(availableBytes(disk))} 可用 · 共 {bytes(disk.total_gb != null ? disk.total_gb * 1024 ** 3 : null)}
                </p>
              </div>
            ))}
            {!disks.length && <p className="muted-copy">暂无磁盘采样</p>}
          </div>
        </article>
        <article className="panel docker-panel">
          <header className="panel-heading">
            <div>
              <p className="section-kicker">Containers</p>
              <h2>Docker</h2>
              <span>{containers.length} 个容器</span>
            </div>
          </header>
          <div className="container-list">
            {containers.slice(0, 10).map((container) => (
              <div key={container.name}>
                <i className={container.status.startsWith('Up') ? 'is-running' : ''} />
                <span>
                  <strong>{container.name}</strong>
                  <small>{container.image}</small>
                </span>
                <b>{container.status}</b>
              </div>
            ))}
            {!containers.length && <p className="muted-copy">Docker 不可用或暂无运行容器</p>}
          </div>
        </article>
      </section>
    </div>
  );
}

/* ---------------- 主组件 ---------------- */

export default function App() {
  const [payload, setPayload] = useState<ServersPayload | null>(null);
  const [view, setView] = useState<View>('overview');
  const [selectedHost, setSelectedHost] = useState('');
  const [serverSearch, setServerSearch] = useState('');
  const [interval, setIntervalValue] = useState(5);
  const [error, setError] = useState('');
  const [range, setRange] = useState<'1h' | '6h' | '24h'>('6h');
  const [metric, setMetric] = useState<MetricKey>('npu_util_percent');
  const [aggregate, setAggregate] = useState<AggregatePayload | null>(null);
  const [modal, setModal] = useState(false);
  const [tagMachine, setTagMachine] = useState<Machine | null>(null);
  const [batchResult, setBatchResult] = useState('');

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/servers', { cache: 'no-store' });
      if (!response.ok) throw new Error(`API ${response.status}`);
      setPayload(await response.json());
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法连接本地采集器');
    }
  }, []);

  const [collectingHost, setCollectingHost] = useState('');
  const collect = useCallback(
    async (host: string) => {
      setCollectingHost(host);
      try {
        await fetch(`/api/collect?host=${encodeURIComponent(host)}`, { method: 'POST' });
        await load();
      } catch {
        /* 失败时下一轮轮询会自愈 */
      } finally {
        setCollectingHost('');
      }
    },
    [load],
  );

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load();
    }, interval * 1000);
    return () => window.clearInterval(timer);
  }, [load, interval]);

  const machines = useMemo(() => Object.values(payload?.machines ?? {}), [payload]);
  const sortedMachines = useMemo(
    () => [...machines].sort((left, right) => left.alias.localeCompare(right.alias, 'zh-CN')),
    [machines],
  );
  const filteredMachines = sortedMachines.filter((machine) =>
    `${machine.alias} ${machine.host}`.toLowerCase().includes(serverSearch.toLowerCase()),
  );
  const selected = payload?.machines[selectedHost] ?? sortedMachines[0];

  const totals = useMemo(() => {
    const devices = machines.flatMap((machine) => machine.npu?.npus ?? []);
    const idle = devices.filter(isIdleDevice).length;
    const hbmUsed = devices.reduce((sum, d) => sum + (d.mem_used_mb ?? 0), 0);
    const hbmTotal = devices.reduce((sum, d) => sum + (d.mem_total_mb ?? 0), 0);
    const utils = devices.map((d) => d.aicore_util ?? 0);
    const avgUtil = utils.length ? utils.reduce((s, v) => s + v, 0) / utils.length : null;
    return {
      servers: machines.length,
      online: machines.filter((m) => m.reachable).length,
      npuCount: devices.length,
      idle,
      busy: devices.length - idle,
      hbmUsed,
      hbmTotal,
      hbmPercent: percent(hbmUsed, hbmTotal),
      avgUtil,
    };
  }, [machines]);

  const alerts = useMemo(
    () =>
      machines
        .flatMap((machine) =>
          (machine.disks ?? [])
            .filter((disk) => disk.use_pct >= 85)
            .map((disk) => ({ machine, disk })),
        )
        .sort((a, b) => b.disk.use_pct - a.disk.use_pct),
    [machines],
  );

  // 历史数据加载（视图与选择变化时）
  const rangeSeconds = range === '1h' ? 3600 : range === '6h' ? 21600 : 86400;
  useEffect(() => {
    if (view !== 'history' || !selected) return;
    const controller = new AbortController();
    const url = `/api/history/aggregate?host=${encodeURIComponent(selected.host)}&range=${rangeSeconds}&bucket=${rangeSeconds <= 7200 ? 600 : 7200}`;
    fetch(url, { cache: 'no-store', signal: controller.signal })
      .then((response) => response.json())
      .then((data: AggregatePayload) => setAggregate(data))
      .catch(() => {
        if (!controller.signal.aborted) setAggregate(null);
      });
    return () => controller.abort();
  }, [view, selected, rangeSeconds, payload]);

  const openServer = (machine: Machine) => {
    setSelectedHost(machine.host);
    setView('server');
  };

  /* ---------------- 服务器管理操作 ---------------- */

  async function action(path: string, method = 'POST', body?: object) {
    await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    await load();
  }

  async function addServers(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const lines = String(form.get('servers') || '')
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    const servers = lines.map((line) => {
      const [name, host, port = '22', username = 'root', tags = ''] = line.split(',').map((value) => value.trim());
      return { name, host: host || name, port: Number(port), username, tags: tags ? tags.split('|') : [] };
    });
    const passwords = String(form.get('passwords') || '')
      .split(/\r?\n/)
      .filter(Boolean);
    setBatchResult('正在逐台检查连接并配置监控密钥…');
    try {
      const response = await fetch('/api/servers/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ servers, passwords }),
      });
      const data = await response.json();
      const ok = (data.results || []).filter((item: { auth: { ok: boolean } }) => item.auth.ok).length;
      setBatchResult(`已完成：${ok}/${servers.length} 台建立密钥连接。一次性密码未保存。`);
      await load();
    } catch (reason) {
      setBatchResult(reason instanceof Error ? reason.message : '批量添加失败');
    }
  }

  async function saveTags(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tagMachine) return;
    const form = new FormData(event.currentTarget);
    const tags = String(form.get('tags') || '')
      .split(/[|,，]/)
      .map((tag) => tag.trim())
      .filter(Boolean);
    await action(`/api/servers/${tagMachine.host}`, 'PUT', { tags });
    setTagMachine(null);
  }

  const titles: Record<View, [string, string]> = {
    overview: ['集群实时总览', '跨服务器资源、空闲设备与基础设施状态'],
    server: [selected?.alias || '服务器详情', selected ? `${selected.user}@${selected.host}:${selected.port}` : '选择服务器查看详情'],
    history: ['资源历史', selected ? `${selected.alias} · 趋势与 2 小时热力图` : '选择服务器查看历史'],
    servers: ['服务器管理', '本地清单、连接状态与采集控制'],
  };

  const historyPoints: TrendPoint[] = useMemo(() => {
    if (!aggregate) return [];
    if (metric === 'npu_util_percent') return aggregate.npu;
    if (metric === 'hbm_percent') return aggregate.npu;
    return aggregate.machine;
  }, [aggregate, metric]);

  const average = (points: TrendPoint[], key: MetricKey) => {
    const values = points.map((p) => p[key]).filter((v): v is number => v != null && Number.isFinite(v));
    return values.length ? values.reduce((s, v) => s + v, 0) / values.length : null;
  };

  const heatmapDays = useMemo(() => buildDays(range === '1h' ? 1 : range === '6h' ? 1 : 7), [range]);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setView('overview')}>
          <span className="brand__mark">N</span>
          <span>
            <strong>NPU Fleet</strong>
            <small>Ascend observability</small>
          </span>
        </button>
        <nav className="primary-nav" aria-label="主导航">
          <button className={view === 'overview' ? 'is-active' : ''} onClick={() => setView('overview')}>
            <i>⌂</i>
            <span>集群总览</span>
            <b>
              {totals.online}/{totals.servers}
            </b>
          </button>
          <button className={view === 'history' ? 'is-active' : ''} onClick={() => setView('history')}>
            <i>▦</i>
            <span>资源历史</span>
          </button>
          <button className={view === 'servers' ? 'is-active' : ''} onClick={() => setView('servers')}>
            <i>⚙</i>
            <span>服务器管理</span>
          </button>
        </nav>
        <div className="sidebar__section">
          <span>服务器</span>
          <b>{totals.online} 在线</b>
        </div>
        <label className="server-search">
          <span>⌕</span>
          <input
            value={serverSearch}
            onChange={(event) => setServerSearch(event.target.value)}
            placeholder="搜索服务器"
            aria-label="搜索服务器"
          />
          {serverSearch && (
            <button onClick={() => setServerSearch('')} aria-label="清空搜索">
              ×
            </button>
          )}
        </label>
        <div className="server-list">
          {filteredMachines.map((machine) => {
            const devices = machine.npu?.npus ?? [];
            return (
              <button
                key={machine.host}
                className={`server-row ${view === 'server' && selected?.host === machine.host ? 'is-selected' : ''}`}
                onClick={() => openServer(machine)}
              >
                <StatusDot status={machine.reachable ? 'online' : 'offline'} />
                <span>
                  <strong>{machine.alias}</strong>
                  <small>
                    {devices.length} NPU · {devices.filter(isIdleDevice).length} 空闲
                  </small>
                </span>
                <i>›</i>
              </button>
            );
          })}
          {!filteredMachines.length && <p className="server-list__empty">没有匹配的服务器</p>}
        </div>
        <div className="collector-card">
          <div>
            <span className="pulse is-working" />
            <strong>{error ? '连接中断' : '采集中'}</strong>
          </div>
          <p>
            {interval} 秒刷新 · {totals.npuCount} 张 NPU
          </p>
          <MiniBar value={error ? 0 : 100} tone="green" />
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p>{titles[view][1]}</p>
            <h1>{titles[view][0]}</h1>
          </div>
          <div className="top-actions">
            <label>
              刷新频率
              <select value={interval} onChange={(event) => setIntervalValue(Number(event.target.value))}>
                {REFRESH_OPTIONS.map((value) => (
                  <option value={value} key={value}>
                    {value} 秒
                  </option>
                ))}
              </select>
            </label>
            <span className={`live-state ${error ? 'is-offline' : ''}`}>
              <i />
              {error ? '采集器离线' : `已同步 · ${timeAgo(machines[0]?.probed_at)}`}
            </span>
            <button className="primary-button" onClick={() => setModal(true)}>
              ＋ 添加服务器
            </button>
          </div>
        </header>
        <div className="workspace-scroll">
          {view === 'overview' && (
            <div className="page-stack">
              <section className="metric-grid" aria-label="集群核心指标">
                <MetricCard
                  label="在线服务器"
                  value={String(totals.online)}
                  suffix={` / ${totals.servers}`}
                  detail={`${totals.npuCount} 张 NPU 已发现`}
                  tone="blue"
                  icon="S"
                />
                <MetricCard
                  label="平均 NPU 利用率"
                  value={number(totals.avgUtil, '%')}
                  detail={`${totals.busy} 张繁忙 · ${totals.idle} 张空闲`}
                  tone="purple"
                  icon="U"
                />
                <MetricCard
                  label="集群 HBM"
                  value={number(totals.hbmPercent, '%')}
                  detail={`${mb(totals.hbmUsed)} / ${mb(totals.hbmTotal)}`}
                  tone="green"
                  icon="M"
                />
                <MetricCard
                  label="可用设备"
                  value={String(totals.idle)}
                  suffix=" 张"
                  detail={`${totals.busy} 张处于繁忙状态`}
                  tone="orange"
                  icon="A"
                />
              </section>
              <section className="fleet-layout">
                <article className="panel fleet-board">
                  <header className="panel-heading">
                    <div>
                      <p className="section-kicker">Fleet</p>
                      <h2>服务器资源矩阵</h2>
                      <span>选择服务器查看每张 NPU、磁盘与容器明细</span>
                    </div>
                    <div className="legend">
                      <span>
                        <i className="legend-hbm" />
                        HBM 占用
                      </span>
                      <span>
                        <i className="legend-active" />
                        AICore 活跃
                      </span>
                    </div>
                  </header>
                  {machines.length ? (
                    <div className="server-grid">
                      {sortedMachines.map((machine) => (
                        <ServerCard
                          key={machine.host}
                          machine={machine}
                          onOpen={() => openServer(machine)}
                          onCollect={() => void collect(machine.host)}
                          collecting={collectingHost === machine.host}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="empty-state">
                      <strong>还没有受管服务器</strong>
                      <p>用 server-management skill 添加服务器后，这里会展示资源矩阵。</p>
                    </div>
                  )}
                </article>
                <aside className="overview-rail">
                  <article className="panel cadence-panel">
                    <header>
                      <span className="icon-tile">↻</span>
                      <div>
                        <h3>自适应采集</h3>
                        <p>{error ? '本地服务不可达' : '页面正在请求实时数据'}</p>
                      </div>
                    </header>
                    <div className="cadence-value">
                      <strong>{interval}</strong>
                      <span>秒</span>
                    </div>
                    <dl>
                      <div>
                        <dt>实时资源</dt>
                        <dd>{interval}s</dd>
                      </div>
                      <div>
                        <dt>基础设施</dt>
                        <dd>300s</dd>
                      </div>
                      <div>
                        <dt>历史写入</dt>
                        <dd>≥ 30s</dd>
                      </div>
                    </dl>
                  </article>
                  <article className="panel alert-panel">
                    <header>
                      <span className="icon-tile icon-tile--orange">!</span>
                      <div>
                        <h3>基础设施提醒</h3>
                        <p>磁盘水位达到 85% 时提示</p>
                      </div>
                    </header>
                    <div className="alert-list">
                      {alerts.slice(0, 5).map(({ machine, disk }) => (
                        <button key={`${machine.host}-${disk.mount}`} onClick={() => openServer(machine)}>
                          <span>
                            <strong>{machine.alias}</strong>
                            <small>{disk.mount}</small>
                          </span>
                          <b>{disk.use_pct}%</b>
                        </button>
                      ))}
                      {!alerts.length && (
                        <div className="all-clear">
                          <i>✓</i>
                          <span>
                            <strong>状态良好</strong>
                            <small>暂无高水位磁盘</small>
                          </span>
                        </div>
                      )}
                    </div>
                  </article>
                </aside>
              </section>
            </div>
          )}

          {view === 'server' && selected && (
            <ServerDetail
              machine={selected}
              onHistory={() => setView('history')}
              onCollect={() => void collect(selected.host)}
              collecting={collectingHost === selected.host}
            />
          )}

          {view === 'history' && selected && (
            <div className="page-stack history-page">
              <section className="history-toolbar panel">
                <div>
                  <label>
                    服务器
                    <select
                      value={selected.host}
                      onChange={(event) => setSelectedHost(event.target.value)}
                    >
                      {sortedMachines.map((machine) => (
                        <option value={machine.host} key={machine.host}>
                          {machine.alias}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    趋势指标
                    <select value={metric} onChange={(event) => setMetric(event.target.value as MetricKey)}>
                      {Object.entries(metricLabels).map(([value, label]) => (
                        <option value={value} key={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="range-tabs" role="group" aria-label="历史范围">
                  {(['1h', '6h', '24h'] as const).map((value) => (
                    <button key={value} className={range === value ? 'is-active' : ''} onClick={() => setRange(value)}>
                      {value}
                    </button>
                  ))}
                </div>
              </section>
              <section className="history-summary metric-grid metric-grid--history">
                <MetricCard label={`${metricLabels[metric]}平均`} value={precise(average(historyPoints, metric), '%')} detail={`${historyPoints.length} 个趋势桶`} tone="blue" icon="Ø" />
                <MetricCard label="NPU 利用率均值" value={precise(average(aggregate?.npu ?? [], 'npu_util_percent'), '%')} detail={`${totals.npuCount} 张 NPU`} tone="purple" icon="N" />
                <MetricCard label="HBM 平均" value={precise(average(aggregate?.npu ?? [], 'hbm_percent'), '%')} detail={`范围 ${range}`} tone="green" icon="H" />
                <MetricCard label="磁盘最高水位" value={number(aggregate?.machine?.length ? Math.max(...aggregate.machine.map((p) => p.disk_max_percent ?? 0)) : null, '%')} detail="窗口内峰值" tone="orange" icon="D" />
              </section>
              <article className="panel trend-panel">
                <header className="panel-heading">
                  <div>
                    <p className="section-kicker">Timeline</p>
                    <h2>{metricLabels[metric]}趋势</h2>
                    <span>{selected.alias} · SQLite 历史数据自动分桶</span>
                  </div>
                </header>
                <TrendCanvas points={historyPoints} metric={metric} />
                <footer className="trend-axis">
                  <span>
                    {historyPoints.length
                      ? new Date(historyPoints[0].bucket * 1000).toLocaleString('zh-CN', { hour12: false })
                      : '暂无数据'}
                  </span>
                  <span>
                    {historyPoints.length
                      ? new Date(historyPoints[historyPoints.length - 1].bucket * 1000).toLocaleString('zh-CN', { hour12: false })
                      : ''}
                  </span>
                </footer>
              </article>
              <section className="heatmap-section">
                <header className="panel-heading">
                  <div>
                    <p className="section-kicker">Activity heatmap</p>
                    <h2>资源活动热力图</h2>
                    <span>每列代表一天、每格代表 2 小时</span>
                  </div>
                </header>
                <div className="heatmap-list">
                  <HistoryHeatmap title="NPU 汇总" subtitle="设备平均 AICore 利用率" points={aggregate?.npu ?? []} days={heatmapDays} tone="purple" valueFor={(p) => p.npu_util_percent ?? null} />
                  <HistoryHeatmap title="HBM 汇总" subtitle="设备显存占用" points={aggregate?.npu ?? []} days={heatmapDays} tone="green" valueFor={(p) => p.hbm_percent ?? null} />
                  <HistoryHeatmap title="CPU" subtitle="整机处理器利用率" points={aggregate?.machine ?? []} days={heatmapDays} tone="blue" valueFor={(p) => p.cpu_percent ?? null} />
                  <HistoryHeatmap title="系统内存" subtitle="整机内存占用" points={aggregate?.machine ?? []} days={heatmapDays} tone="orange" valueFor={(p) => p.memory_percent ?? null} />
                </div>
              </section>
            </div>
          )}

          {view === 'servers' && (
            <section className="panel server-table">
              <header className="panel-heading">
                <div>
                  <p className="section-kicker">Inventory</p>
                  <h2>已纳管服务器</h2>
                  <span>标签可用于搜索和分组；移除仅清理本地登记</span>
                </div>
              </header>
              {machines.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>服务器</th>
                        <th>连接地址</th>
                        <th>设备</th>
                        <th>标签</th>
                        <th>最近采样</th>
                        <th>状态</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedMachines.map((machine) => (
                        <tr key={machine.host}>
                          <td>
                            <button className="table-server" onClick={() => openServer(machine)}>
                              <StatusDot status={machine.reachable ? 'online' : 'offline'} />
                              <strong>{machine.alias}</strong>
                            </button>
                          </td>
                          <td>
                            <code>
                              {machine.user}@{machine.host}:{machine.port}
                            </code>
                          </td>
                          <td>{machine.npu?.npu_count ?? 0} NPU</td>
                          <td>
                            <div className="table-tags">
                              <ServerTags tags={machine.tags ?? []} />
                              <button onClick={() => setTagMachine(machine)}>编辑标签</button>
                            </div>
                          </td>
                          <td>{timeAgo(machine.probed_at)}</td>
                          <td>
                            <span className={`status-label ${machine.enabled === false ? '' : machine.reachable ? 'status-label--online' : 'status-label--offline'}`}>
                              {machine.enabled === false ? '已暂停' : machine.reachable ? '在线' : '待检查'}
                            </span>
                          </td>
                          <td className="row-actions">
                            <button onClick={() => void action(`/api/servers/${machine.host}`, 'PUT', { enabled: machine.enabled === false })}>
                              {machine.enabled === false ? '启用' : '暂停'}
                            </button>
                            <button
                              className="is-danger"
                              onClick={() => {
                                if (confirm(`移除 ${machine.alias}？历史数据保留，仅清理本地登记。`)) {
                                  void action(`/api/servers/${machine.host}`, 'DELETE');
                                }
                              }}
                            >
                              移除
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="empty-state">
                  <strong>服务器列表为空</strong>
                  <button className="primary-button" onClick={() => setModal(true)}>
                    批量添加
                  </button>
                </div>
              )}
            </section>
          )}
        </div>
      </section>

      {modal && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setModal(false)}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="add-title">
            <header>
              <div>
                <p className="section-kicker">Batch onboarding</p>
                <h2 id="add-title">添加 NPU 服务器</h2>
              </div>
              <button aria-label="关闭" onClick={() => setModal(false)}>
                ×
              </button>
            </header>
            <form onSubmit={addServers}>
              <label>
                服务器列表
                <span>每行：名称, 主机, 端口, 用户, 标签1|标签2</span>
                <textarea name="servers" required rows={7} placeholder={'atlas-a3-01, 10.18.4.21, 22, root, A3|训练\natlas-a2-07, 10.18.4.37, 22, root, A2'} />
              </label>
              <label>
                一次性密码候选
                <span>可选，每行一个；仅在本次请求内按顺序尝试</span>
                <textarea name="passwords" rows={4} autoComplete="new-password" spellCheck={false} />
              </label>
              <div className="security-note">
                <b>安全连接策略</b>
                <p>先检查已有密钥；失败后才尝试候选密码。成功后安装本机 Ed25519 公钥，后续只使用密钥。密码不保存、不回显。</p>
              </div>
              {batchResult && <p className="form-result">{batchResult}</p>}
              <footer>
                <button type="button" className="secondary-button" onClick={() => setModal(false)}>
                  关闭
                </button>
                <button type="submit" className="primary-button">
                  开始检查并添加
                </button>
              </footer>
            </form>
          </section>
        </div>
      )}

      {tagMachine && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setTagMachine(null)}>
          <section className="modal modal--tags" role="dialog" aria-modal="true" aria-labelledby="tag-title">
            <header>
              <div>
                <p className="section-kicker">Server tags</p>
                <h2 id="tag-title">编辑 {tagMachine.alias} 的标签</h2>
              </div>
              <button aria-label="关闭" onClick={() => setTagMachine(null)}>
                ×
              </button>
            </header>
            <form onSubmit={saveTags}>
              <label>
                服务器标签
                <span>使用竖线、逗号或中文逗号分隔，最多 20 个</span>
                <input name="tags" autoFocus defaultValue={(tagMachine.tags ?? []).join(' | ')} placeholder="A3 | 训练 | 北京机房" />
              </label>
              <footer>
                <button type="button" className="secondary-button" onClick={() => setTagMachine(null)}>
                  取消
                </button>
                <button type="submit" className="primary-button">
                  保存标签
                </button>
              </footer>
            </form>
          </section>
        </div>
      )}
    </main>
  );
}
