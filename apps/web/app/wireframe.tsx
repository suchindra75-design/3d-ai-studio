/**
 * Static wireframe of a subdivided icosahedron with camera markers around it,
 * computed at build time (no client JavaScript). Depth controls stroke opacity.
 */

type Vec3 = [number, number, number];

function normalize([x, y, z]: Vec3): Vec3 {
  const length = Math.hypot(x, y, z);
  return [x / length, y / length, z / length];
}

function icosphere(subdivisions: number): { vertices: Vec3[]; edges: [number, number][] } {
  const t = (1 + Math.sqrt(5)) / 2;
  let vertices: Vec3[] = (
    [
      [-1, t, 0],
      [1, t, 0],
      [-1, -t, 0],
      [1, -t, 0],
      [0, -1, t],
      [0, 1, t],
      [0, -1, -t],
      [0, 1, -t],
      [t, 0, -1],
      [t, 0, 1],
      [-t, 0, -1],
      [-t, 0, 1],
    ] as Vec3[]
  ).map(normalize);
  let faces: [number, number, number][] = [
    [0, 11, 5],
    [0, 5, 1],
    [0, 1, 7],
    [0, 7, 10],
    [0, 10, 11],
    [1, 5, 9],
    [5, 11, 4],
    [11, 10, 2],
    [10, 7, 6],
    [7, 1, 8],
    [3, 9, 4],
    [3, 4, 2],
    [3, 2, 6],
    [3, 6, 8],
    [3, 8, 9],
    [4, 9, 5],
    [2, 4, 11],
    [6, 2, 10],
    [8, 6, 7],
    [9, 8, 1],
  ];
  for (let s = 0; s < subdivisions; s += 1) {
    const cache = new Map<string, number>();
    const midpoint = (a: number, b: number) => {
      const key = a < b ? `${a}-${b}` : `${b}-${a}`;
      const hit = cache.get(key);
      if (hit !== undefined) return hit;
      const [va, vb] = [vertices[a]!, vertices[b]!];
      vertices = [
        ...vertices,
        normalize([(va[0] + vb[0]) / 2, (va[1] + vb[1]) / 2, (va[2] + vb[2]) / 2]),
      ];
      cache.set(key, vertices.length - 1);
      return vertices.length - 1;
    };
    faces = faces.flatMap(([a, b, c]) => {
      const ab = midpoint(a, b);
      const bc = midpoint(b, c);
      const ca = midpoint(c, a);
      return [
        [a, ab, ca],
        [b, bc, ab],
        [c, ca, bc],
        [ab, bc, ca],
      ] as [number, number, number][];
    });
  }
  const edgeSet = new Set<string>();
  const edges: [number, number][] = [];
  for (const [a, b, c] of faces) {
    for (const [p, q] of [
      [a, b],
      [b, c],
      [c, a],
    ] as [number, number][]) {
      const key = p < q ? `${p}-${q}` : `${q}-${p}`;
      if (!edgeSet.has(key)) {
        edgeSet.add(key);
        edges.push([p, q]);
      }
    }
  }
  return { vertices, edges };
}

function rotate([x, y, z]: Vec3, yaw: number, pitch: number): Vec3 {
  const [cy, sy, cp, sp] = [Math.cos(yaw), Math.sin(yaw), Math.cos(pitch), Math.sin(pitch)];
  const x1 = x * cy + z * sy;
  const z1 = -x * sy + z * cy;
  return [x1, y * cp - z1 * sp, y * sp + z1 * cp];
}

export function Wireframe({ className }: { className?: string }) {
  const { vertices, edges } = icosphere(2);
  const size = 520;
  const radius = 170;
  const center = size / 2;
  const projected = vertices.map((v) => {
    const [x, y, z] = rotate(v, 0.55, -0.35);
    return { x: center + x * radius, y: center - y * radius, z };
  });
  const cameras = Array.from({ length: 10 }, (_, i) => {
    const angle = (i / 10) * Math.PI * 2;
    const [x, y, z] = rotate([Math.cos(angle) * 1.45, 0.25, Math.sin(angle) * 1.45], 0.55, -0.35);
    return { x: center + x * radius, y: center - y * radius, z };
  });

  return (
    <svg viewBox={`0 0 ${size} ${size}`} className={className} aria-hidden>
      <ellipse
        cx={center}
        cy={center + radius + 28}
        rx={radius * 0.9}
        ry={14}
        fill="white"
        opacity={0.03}
      />
      {edges.map(([a, b]) => {
        const pa = projected[a]!;
        const pb = projected[b]!;
        const depth = (pa.z + pb.z) / 2; // -1 (back) … 1 (front)
        return (
          <line
            key={`${a}-${b}`}
            x1={pa.x}
            y1={pa.y}
            x2={pb.x}
            y2={pb.y}
            stroke="white"
            strokeWidth={depth > 0 ? 1 : 0.75}
            opacity={0.08 + Math.max(0, depth) * 0.4}
          />
        );
      })}
      {cameras.map((cam, i) => (
        <g key={i} opacity={cam.z > 0 ? 0.95 : 0.35}>
          <line
            x1={cam.x}
            y1={cam.y}
            x2={center}
            y2={center}
            stroke="#0a84ff"
            strokeWidth={0.6}
            opacity={0.25}
            strokeDasharray="2 4"
          />
          <rect
            x={cam.x - 5}
            y={cam.y - 3.5}
            width={10}
            height={7}
            rx={1.5}
            fill="#0b0b0c"
            stroke="#0a84ff"
            strokeWidth={1.2}
          />
        </g>
      ))}
    </svg>
  );
}
