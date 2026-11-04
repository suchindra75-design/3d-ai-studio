import '@ai3d/ui/fonts';
import './styles.css';

import { createApiClient, type ApiClient } from '@ai3d/api-client';
import {
  AboutScreen,
  AppShell,
  NewProjectScreen,
  NotFoundScreen,
  ModelWorkspaceScreen,
  ProjectWorkspaceScreen,
  ProjectsScreen,
  SettingsScreen,
  StudioProvider,
  createQueryClient,
  routes,
  type RuntimeFact,
  type StudioContextValue,
  type StudioLinkProps,
} from '@ai3d/studio';
import { QueryClientProvider } from '@tanstack/react-query';
import { Button, Spinner } from '@ai3d/ui';
import {
  StrictMode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { createRoot } from 'react-dom/client';
import {
  Link as RouterLink,
  Navigate,
  Outlet,
  RouterProvider,
  createHashRouter,
  useLocation,
  useNavigate,
  useParams,
} from 'react-router';

import {
  BackendFailedError,
  connectBackend,
  desktopFeatures,
  loadRuntimeFacts,
  restartBackend,
  type BackendView,
  type Connection,
} from './runtime';

const queryClient = createQueryClient();

interface Session {
  api: ApiClient;
  connection: Connection;
}
const SessionContext = createContext<Session | null>(null);

function Link({ href, ...props }: StudioLinkProps) {
  return <RouterLink to={href} {...props} />;
}

function useRouterPathname(): string {
  return useLocation().pathname;
}

function Root() {
  const navigate = useNavigate();
  const { api, connection } = useContext(SessionContext)!;
  const [runtime, setRuntime] = useState<RuntimeFact[]>([]);
  useEffect(() => {
    loadRuntimeFacts(connection).then(setRuntime, (error: unknown) =>
      setRuntime([{ label: 'Desktop shell', value: `Unavailable (${String(error)})` }]),
    );
  }, [connection]);
  const value = useMemo<StudioContextValue>(
    () => ({
      api,
      platform: 'desktop',
      desktop: desktopFeatures,
      runtime,
      navigation: {
        Link,
        push: (href) => void navigate(href),
        replace: (href) => void navigate(href, { replace: true }),
        usePathname: useRouterPathname,
      },
    }),
    [api, navigate, runtime],
  );
  return (
    <StudioProvider value={value}>
      <AppShell>
        <Outlet />
      </AppShell>
    </StudioProvider>
  );
}

function ProjectRoute() {
  const { projectId = '' } = useParams();
  return <ProjectWorkspaceScreen key={projectId} projectId={projectId} />;
}

function ModelRoute() {
  const { projectId = '', modelId = '' } = useParams();
  return <ModelWorkspaceScreen key={modelId} projectId={projectId} modelId={modelId} />;
}

// Hash routing: the bundled app is served from a custom protocol with no server rewrites.
const router = createHashRouter([
  {
    element: <Root />,
    children: [
      { index: true, element: <Navigate to={routes.projects} replace /> },
      { path: routes.projects, element: <ProjectsScreen /> },
      { path: routes.newProject, element: <NewProjectScreen /> },
      { path: '/projects/:projectId', element: <ProjectRoute /> },
      { path: '/projects/:projectId/models/:modelId', element: <ModelRoute /> },
      { path: routes.settings, element: <SettingsScreen /> },
      { path: routes.about, element: <AboutScreen /> },
      { path: '*', element: <NotFoundScreen /> },
    ],
  },
]);

/** Shown until the local engine answers (first start runs database migrations). */
function EngineSplash({
  view,
  error,
  onRetry,
}: {
  view: BackendView | null;
  error: unknown;
  onRetry: () => void;
}) {
  const failed = error instanceof BackendFailedError ? error.view : null;
  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <div className="flex w-full max-w-lg flex-col gap-4">
        {error ? (
          <div role="alert" className="flex flex-col gap-3">
            <h1 className="text-lg font-semibold">The local engine did not start</h1>
            <p className="text-sm text-muted-foreground">
              {error instanceof Error ? error.message : String(error)}
            </p>
            {failed && failed.log_tail.length > 0 ? (
              <pre className="max-h-48 overflow-auto rounded-md bg-surface p-3 font-mono text-[11px] text-muted-foreground">
                {failed.log_tail.join('\n')}
              </pre>
            ) : null}
            {view ? (
              <p className="text-xs text-subtle-foreground">
                Full log: <code className="font-mono">{view.log_path}</code>
              </p>
            ) : null}
            <Button variant="primary" className="self-start" onClick={onRetry}>
              Try again
            </Button>
          </div>
        ) : (
          <div role="status" className="flex items-center gap-3 text-sm text-muted-foreground">
            <Spinner label="Starting" />
            Starting the local engine…
          </div>
        )}
      </div>
    </main>
  );
}

function Boot() {
  const [session, setSession] = useState<Session | null>(null);
  const [view, setView] = useState<BackendView | null>(null);
  const [error, setError] = useState<unknown>(null);

  const connect = useCallback(() => {
    connectBackend(setView).then(
      (connection) =>
        setSession({
          connection,
          api: createApiClient({ baseUrl: connection.baseUrl, token: connection.token }),
        }),
      setError,
    );
  }, []);
  useEffect(connect, [connect]);

  function retry() {
    setError(null);
    void restartBackend().finally(connect);
  }

  if (!session) {
    return <EngineSplash view={view} error={error} onRetry={retry} />;
  }
  return (
    <SessionContext.Provider value={session}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </SessionContext.Provider>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Boot />
  </StrictMode>,
);
