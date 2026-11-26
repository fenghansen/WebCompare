import { api } from './api';
import { t } from './i18n.js';

const reserve = 512 * 1024 ** 2;
type Sample = { available: number | null; spent: number; time: number };
let sample: Sample | undefined;
let reading: Promise<Sample> | undefined;
let inFlightBytes = 0;

export class MediaMemoryError extends Error {
  constructor() {
    super(t('可用物理内存不足，继续处理将低于 512 MiB 预留。释放内存后重试。'));
  }
}

function sampleMemory(force = false): Promise<Sample> {
  if (reading) return reading;
  if (!force && sample && performance.now() - sample.time < (sample.available === null ? 10000 : 250)) {
    return Promise.resolve(sample);
  }
  reading = api.personalMemory().then(({ available_bytes }) => {
    sample = { available: available_bytes, time: performance.now(), spent: 0 };
    return sample;
  }).finally(() => { reading = undefined; });
  return reading;
}

export async function checkMediaMemory(bytes = 0): Promise<void> {
  let current = await sampleMemory();
  const insufficient = (s: Sample) => s.available !== null && s.available - s.spent - inFlightBytes - bytes < reserve;
  // Fetched once per burst. Re-read before refusing on estimates for already completed work.
  if (insufficient(current) && current.spent > 0) current = await sampleMemory(true);
  if (insufficient(current)) throw new MediaMemoryError();
}

export async function withMediaMemory<T>(bytes: number, allocate: () => Promise<T>): Promise<T> {
  // Two decoders can start before their native allocations appear in MemAvailable.
  inFlightBytes += bytes;
  try {
    await checkMediaMemory();
    const result = await allocate();
    if (sample && sample.available !== null) sample.spent += bytes;
    return result;
  } finally {
    inFlightBytes -= bytes;
  }
}
