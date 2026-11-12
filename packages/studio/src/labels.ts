import type {
  InputMode,
  SceneRead,
  JobStatus,
  ModelVersionSource,
  ProcessingMode,
  QualityLevel,
  QualityMode,
  ReportStatus,
  SurfaceProvenance,
} from '@ai3d/shared-types';

type SceneShot = SceneRead['shot'];
type SceneLighting = SceneRead['lighting'];
type SceneGround = SceneRead['ground'];
type SceneStyle = SceneRead['style'];
type SceneAngle = SceneRead['angle'];

export const inputModeLabel: Record<InputMode, string> = {
  single_image: 'Single photo',
  multi_view: 'Multiple photos',
  prompt: 'From a prompt',
};

export const qualityModeLabel: Record<QualityMode, string> = {
  fast: 'Fast',
  high_quality: 'High Quality',
  professional: 'Professional',
};

export const processingModeLabel: Record<ProcessingMode, string> = {
  auto: 'Automatic',
  local: 'Local',
  cloud: 'Cloud',
};

export const qualityLevelLabel: Record<QualityLevel, string> = {
  good: 'Good',
  warning: 'Check',
  poor: 'Poor',
};

export const qualityLevelTone = {
  good: 'success',
  warning: 'warning',
  poor: 'danger',
} as const satisfies Record<QualityLevel, 'success' | 'warning' | 'danger'>;

export const jobStatusLabel: Record<JobStatus, string> = {
  waiting_for_consent: 'Waiting for permission',
  queued: 'Queued',
  running: 'Running',
  cancelling: 'Cancelling',
  succeeded: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export const jobStatusTone = {
  waiting_for_consent: 'warning',
  queued: 'neutral',
  running: 'accent',
  cancelling: 'neutral',
  succeeded: 'success',
  failed: 'danger',
  cancelled: 'neutral',
} as const satisfies Record<JobStatus, 'neutral' | 'accent' | 'success' | 'warning' | 'danger'>;

export const jobStageLabel: Record<string, string> = {
  preparing: 'Preparing photos',
  loading: 'Opening the model',
  processing: 'Processing',
  submitting: 'Sending photos',
  features: 'Finding features',
  matching: 'Matching photos',
  mapping: 'Placing cameras',
  filtering: 'Filtering points',
  meshing: 'Building the surface',
  refining: 'Refining the model',
  texturing: 'Texturing',
  generating: 'Generating',
  downloading: 'Downloading result',
  validating: 'Checking the 3D file',
  saving: 'Saving',
  planning: 'Planning the scene',
  painting: 'Painting a reference picture',
  shaping: 'Shaping an object',
  arranging: 'Setting the stage',
  rendering: 'Rendering',
  done: 'Done',
};

// Scene choices (D76): what the renderer can do; "auto" leaves it to the scene planner.
export const sceneShotLabel: Record<SceneShot, string> = {
  orbit: 'Orbit around',
  push_in: 'Push in',
  pull_out: 'Pull out',
  pan: 'Pan across',
  crane_up: 'Crane up',
  fly_by: 'Fly by',
  static: 'Locked off',
};

export const sceneLightingLabel: Record<SceneLighting, string> = {
  studio: 'Studio',
  daylight: 'Daylight',
  golden_hour: 'Golden hour',
  overcast: 'Overcast',
  night: 'Night',
  neon: 'Neon',
  space: 'Space',
};

export const sceneGroundLabel: Record<SceneGround, string> = {
  studio: 'Studio backdrop',
  grass: 'Grass',
  sand: 'Sand',
  concrete: 'Concrete',
  water: 'Water',
  snow: 'Snow',
  none: 'None',
};

export const sceneStyleLabel: Record<SceneStyle, string> = {
  cinematic: 'Cinematic',
  realistic: 'Realistic',
  stylised: 'Stylised',
  miniature: 'Miniature',
};

export const sceneAngleLabel: Record<SceneAngle, string> = {
  eye: 'Eye level',
  low: 'Low angle',
  high: 'High angle',
  top: 'From above',
};

export const viewRoleLabel: Record<string, string> = {
  source: 'Photo',
  front: 'Front',
  left: 'Left',
  back: 'Back',
  right: 'Right',
};

export const provenanceLabel: Record<SurfaceProvenance, string> = {
  inferred: 'AI-generated: unseen surfaces invented',
  measured: 'Measured from photos',
  mixed: 'Partly measured, partly AI-generated',
  unknown: 'Origin unknown',
};

export const reportStatusLabel: Record<ReportStatus, string> = {
  pass: 'File checks passed',
  warn: 'File checks: warnings',
  fail: 'File checks failed',
};

export const reportStatusTone = {
  pass: 'success',
  warn: 'warning',
  fail: 'danger',
} as const satisfies Record<ReportStatus, 'success' | 'warning' | 'danger'>;

export const versionSourceLabel: Record<ModelVersionSource, string> = {
  generated: 'Generated',
  reconstructed: 'Reconstructed',
  enhanced: 'Enhanced',
  imported: 'Imported',
  edited: 'Edited',
};
