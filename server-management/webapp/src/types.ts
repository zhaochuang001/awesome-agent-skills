// 与后端 fleet_service.py 输出 JSON 对应的数据类型。

export type Chip = {
  name?: string | null;
  health?: string | null;
  power_w?: number | null;
  temp_c?: number | null;
  bus_id?: string | null;
  aicore_util?: number | null;
  mem_used_mb?: number | null;
  mem_total_mb?: number | null;
};

export type NpuProcess = {
  pid: number;
  name: string;
  memory_mb: number;
};

export type Device = {
  id: number;
  name?: string | null;
  health?: string | null;
  power_w?: number | null;
  temp_c?: number | null;
  bus_id?: string | null;
  aicore_util?: number | null;
  mem_used_mb?: number | null;
  mem_total_mb?: number | null;
  chips: Chip[];
  processes: NpuProcess[];
};

export type Disk = {
  filesystem: string;
  mount: string;
  total_gb: number | null;
  used_gb: number | null;
  use_pct: number;
};

export type Container = {
  name: string;
  status: string;
  image: string;
};

export type Machine = {
  alias: string;
  host: string;
  port: number;
  user: string;
  reachable: boolean;
  probed_at: string;
  error?: string;
  npu?: { npu_count: number; npus: Device[] };
  npu_idle?: number;
  load1?: number | null;
  cpu_percent?: number | null;
  memory_available_bytes?: number | null;
  memory_total_bytes?: number | null;
  disks?: Disk[] | null;
  docker?: Container[] | null;
  extras_probed_at?: string;
  extras_error?: string;
  tags?: string[];
  enabled?: boolean;
};

export type Summary = {
  machines_total: number;
  machines_reachable: number;
  npu_total: number;
  npu_healthy: number;
};

export type ServersPayload = {
  machines: Record<string, Machine>;
  summary: Summary;
};

export type AggregateBucket = {
  bucket: number;
  npu_util_percent?: number | null;
  hbm_percent?: number | null;
  temp_c?: number | null;
  power_w?: number | null;
  sample_count: number;
};

export type MachineBucket = {
  bucket: number;
  cpu_percent?: number | null;
  memory_percent?: number | null;
  load1?: number | null;
  disk_max_percent?: number | null;
  sample_count: number;
};

export type AggregatePayload = {
  host: string;
  range: number;
  bucket: number;
  npu: AggregateBucket[];
  machine: MachineBucket[];
};
