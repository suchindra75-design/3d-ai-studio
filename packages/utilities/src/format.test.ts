import { formatBytes, formatPercent, formatRelativeTime, pluralize } from './format';

describe('formatBytes', () => {
  it.each([
    [0, '0 B'],
    [1023, '1023 B'],
    [1536, '1.5 KB'],
    [52_428_800, '50.0 MB'],
    [1.5 * 1024 ** 3, '1.5 GB'],
    [250 * 1024 ** 2, '250 MB'],
  ])('%d -> %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it('handles missing and invalid values', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(-1)).toBe('—');
    expect(formatBytes(Number.NaN)).toBe('—');
  });
});

describe('formatRelativeTime', () => {
  const now = new Date('2026-11-03T12:00:00Z');

  it('says "just now" for recent times', () => {
    expect(formatRelativeTime('2026-11-03T11:59:40Z', now)).toBe('just now');
  });

  it('uses the largest fitting unit', () => {
    expect(formatRelativeTime('2026-11-03T11:55:00Z', now)).toMatch(/5 minutes ago/);
    expect(formatRelativeTime('2026-11-03T09:00:00Z', now)).toMatch(/3 hours ago/);
    expect(formatRelativeTime('2026-10-01T12:00:00Z', now)).toMatch(/last month|1 month ago/);
  });

  it('rejects invalid dates', () => {
    expect(formatRelativeTime('not a date', now)).toBe('—');
  });
});

it('pluralizes and formats percentages', () => {
  expect(pluralize(1, 'photo')).toBe('1 photo');
  expect(pluralize(3, 'photo')).toBe('3 photos');
  expect(pluralize(0, 'child', 'children')).toBe('0 children');
  expect(formatPercent(0.4567)).toBe('46%');
  expect(formatPercent(0.0123, 1)).toBe('1.2%');
});
