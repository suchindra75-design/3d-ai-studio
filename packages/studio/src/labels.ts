import type {
  InputMode,
  JobStatus,
  ModelVersionSource,
  ProcessingMode,
  QualityLevel,
  QualityMode,
  ReportStatus,
  SurfaceProvenance,
} from '@ai3d/shared-types';

export const inputModeLabel: Record<InputMode, string> = {
  single_image: 'Single photo',
  multi_view: 'Multiple photos',
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
  texturing: 'Texturing',
  generating: 'Generating',
  downloading: 'Downloading result',
  validating: 'Checking the 3D file',
  saving: 'Saving',
  done: 'Done',
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
