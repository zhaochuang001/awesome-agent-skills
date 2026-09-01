// 展示格式化工具（对齐 vaws-top 的呈现习惯）。

import type { Device } from './types';

export function number(value: number | null | undefined, suffix = '') {
  return value == null || !Number.isFinite(value) ? '—' : `${Math.round(value)}${suffix}`;
}

export function precise(value: number | null | undefined, suffix = '') {
  return value == null || !Number.isFinite(value) ? '—' : `${value.toFixed(1)}${suffix}`;
}

export function bytes(value: number | null | undefined) {
  if (value == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index < 2 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

export function mb(value: number | null | undefined) {
  if (value == null) return '—';
  return bytes(value * 1024 * 1024);
}

export function percent(used?: number | null, total?: number | null) {
  return used != null && total ? Math.max(0, Math.min(100, (used * 100) / total)) : null;
}

export function clampPercent(value?: number | null) {
  return value == null || !Number.isFinite(value) ? 0 : Math.max(0, Math.min(100, value));
}

export function timeAgo(iso?: string) {
  if (!iso) return '尚未采集';
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - new Date(iso).getTime() / 1000));
  if (delta < 5) return '刚刚';
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

export function isIdleDevice(device: Device): boolean {
  if (device.health !== 'OK') return false;
  if (device.aicore_util != null && device.aicore_util > 5) return false;
  if (device.mem_used_mb != null && device.mem_total_mb) {
    if (device.mem_used_mb / device.mem_total_mb > 0.05) return false;
  }
  return true;
}
