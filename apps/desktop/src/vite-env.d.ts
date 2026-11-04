/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Browser preview only (vite dev, e2e): where the backend is. The app finds its own. */
  readonly VITE_AI3D_API_URL?: string;
  /** Browser preview only: token of a backend started with AI3D_API_TOKEN. */
  readonly VITE_AI3D_API_TOKEN?: string;
}
