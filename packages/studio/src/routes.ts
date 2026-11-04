/** Route paths shared by the web (Next.js) and desktop (React Router) apps. */
export const routes = {
  projects: '/projects',
  newProject: '/projects/new',
  project: (id: string) => `/projects/${id}`,
  model: (projectId: string, modelId: string) => `/projects/${projectId}/models/${modelId}`,
  settings: '/settings',
  about: '/about',
} as const;
