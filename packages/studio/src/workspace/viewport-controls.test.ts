import { formatTriangles } from './viewport-controls';

it('shortens triangle counts without rounding up to "1000K"', () => {
  expect(formatTriangles(999_698)).toBe('1.0M');
  expect(formatTriangles(999_499)).toBe('999K');
  expect(formatTriangles(5_242_880)).toBe('5.2M');
  expect(formatTriangles(81_920)).toBe('82K');
  expect(formatTriangles(512)).toBe('512');
});
