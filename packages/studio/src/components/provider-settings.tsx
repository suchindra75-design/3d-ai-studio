'use client';

import type { ProviderRead } from '@ai3d/shared-types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FieldHint,
  Input,
  Label,
  Skeleton,
} from '@ai3d/ui';
import { CircleCheck, CircleX, ExternalLink } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';

import {
  useCheckProvider,
  useDeleteProviderBaseUrl,
  useDeleteProviderKey,
  useProviders,
  useSetProviderBaseUrl,
  useSetProviderKey,
} from '../api/hooks';
import { ErrorState } from './page';

function ServerAddress({ provider }: { provider: ProviderRead }) {
  const inputId = useId();
  const [address, setAddress] = useState('');
  const save = useSetProviderBaseUrl();
  const remove = useDeleteProviderBaseUrl();

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!address.trim()) return;
    save.mutate(
      { key: provider.key, baseUrl: address.trim() },
      { onSuccess: () => setAddress('') },
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs">
        {provider.base_url ? (
          <>
            Server: <code className="font-mono">{provider.base_url}</code>
            {provider.base_url_source === 'environment' ? (
              <>
                {' '}
                (from <code className="font-mono">{provider.base_url_env_var}</code>)
              </>
            ) : null}
          </>
        ) : (
          <>
            No server address. Add one below or set{' '}
            <code className="font-mono">{provider.base_url_env_var}</code> for the backend.
          </>
        )}
      </p>
      <form onSubmit={submit} className="flex flex-col gap-1.5">
        <Label htmlFor={inputId}>
          {provider.base_url ? 'Change server address' : 'Server address'}
        </Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id={inputId}
            type="url"
            autoComplete="off"
            spellCheck={false}
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            className="min-w-0 flex-1"
            placeholder="https://your-gpu-server:8800"
          />
          <Button type="submit" variant="secondary" disabled={!address.trim() || save.isPending}>
            Save address
          </Button>
          {provider.base_url_source === 'stored' ? (
            <Button
              type="button"
              variant="ghost"
              disabled={remove.isPending}
              onClick={() => remove.mutate(provider.key)}
            >
              Remove address
            </Button>
          ) : null}
        </div>
        <FieldHint>
          Use https:// (the access token goes with every request); http:// only for this computer,
          e.g. an SSH tunnel.
        </FieldHint>
      </form>
      {save.isError ? <ErrorState error={save.error} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}
    </div>
  );
}

function KeyStatus({ provider }: { provider: ProviderRead }) {
  if (!provider.requires_api_key) {
    return provider.unavailable_reason ? (
      <span className="text-warning">{provider.unavailable_reason}</span>
    ) : (
      <span>No API key needed; runs on this computer.</span>
    );
  }
  if (provider.credential_error) {
    return <span className="text-danger">{provider.credential_error}</span>;
  }
  const what = provider.requires_base_url ? 'token' : 'key';
  const What = provider.requires_base_url ? 'Access token' : 'Key';
  switch (provider.credential_source) {
    case 'environment':
      return (
        <span>
          {What} from the <code className="font-mono">{provider.api_key_env_var}</code> environment
          variable ({provider.api_key_hint}).
        </span>
      );
    case 'stored':
      return (
        <span>
          {What} stored encrypted on this machine ({provider.api_key_hint}).
        </span>
      );
    default:
      return (
        <span>
          No {what}. Add one below or set{' '}
          <code className="font-mono">{provider.api_key_env_var}</code> for the backend.
        </span>
      );
  }
}

function ProviderRow({ provider }: { provider: ProviderRead }) {
  const inputId = useId();
  const [apiKey, setApiKey] = useState('');
  const save = useSetProviderKey();
  const remove = useDeleteProviderKey();
  const check = useCheckProvider();
  const server = provider.requires_base_url;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!apiKey.trim()) return;
    check.reset();
    save.mutate({ key: provider.key, apiKey: apiKey.trim() }, { onSuccess: () => setApiKey('') });
  }

  return (
    <li className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{provider.display_name}</span>
        {provider.kind === 'mock' ? (
          <Badge variant="warning">Test provider</Badge>
        ) : (
          <Badge variant="outline">
            {server ? 'Your server' : provider.uploads_images ? 'Cloud' : 'Local'}
          </Badge>
        )}
        {provider.configured ? (
          <Badge variant="success">
            <CircleCheck className="size-3" aria-hidden /> Ready
          </Badge>
        ) : (
          <Badge>
            <CircleX className="size-3" aria-hidden />{' '}
            {provider.requires_api_key ? 'Not configured' : 'Not installed'}
          </Badge>
        )}
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">{provider.description}</p>
      <ul className="flex flex-col gap-1 text-xs text-subtle-foreground">
        {provider.capabilities.notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
      {provider.pricing_url || provider.docs_url ? (
        <p className="flex flex-wrap gap-3 text-xs">
          {provider.pricing_url ? (
            <a
              href={provider.pricing_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-accent hover:underline"
            >
              Pricing <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : null}
          {provider.docs_url ? (
            <a
              href={provider.docs_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-accent hover:underline"
            >
              API documentation <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : null}
        </p>
      ) : null}
      {server ? <ServerAddress provider={provider} /> : null}
      <p className="text-xs">
        <KeyStatus provider={provider} />
      </p>

      {provider.requires_api_key ? (
        <form onSubmit={submit} className="flex flex-col gap-1.5">
          <Label htmlFor={inputId}>
            {server
              ? provider.credential_source === 'none'
                ? 'Access token'
                : 'Replace stored token'
              : provider.credential_source === 'none'
                ? 'API key'
                : 'Replace stored key'}
          </Label>
          <div className="flex flex-wrap gap-2">
            <Input
              id={inputId}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              className="min-w-0 flex-1"
              placeholder={
                provider.key === 'tripo' ? 'tsk_…' : provider.key === 'meshy' ? 'msy_…' : ''
              }
            />
            <Button type="submit" variant="secondary" disabled={!apiKey.trim() || save.isPending}>
              {server ? 'Save token' : 'Save key'}
            </Button>
          </div>
          <FieldHint>
            Sent once to the local backend and stored encrypted. It is never shown again or included
            in exports.
          </FieldHint>
        </form>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {provider.configured ? (
          <Button
            size="sm"
            variant="secondary"
            disabled={check.isPending}
            onClick={() => check.mutate(provider.key)}
          >
            {check.isPending ? 'Checking…' : server ? 'Check connection' : 'Check key'}
          </Button>
        ) : null}
        {provider.credential_source === 'stored' ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={remove.isPending}
            onClick={() => {
              check.reset();
              remove.mutate(provider.key);
            }}
          >
            {server ? 'Remove stored token' : 'Remove stored key'}
          </Button>
        ) : null}
        {check.data ? (
          <span
            role="status"
            className={check.data.ok ? 'text-xs text-success' : 'text-xs text-danger'}
          >
            {check.data.message}
          </span>
        ) : null}
      </div>
      {save.isError ? <ErrorState error={save.error} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}
      {check.isError ? <ErrorState error={check.error} /> : null}
    </li>
  );
}

export function ProviderSettings() {
  const providers = useProviders();
  return (
    <Card>
      <CardHeader>
        <CardTitle>AI providers</CardTitle>
        <CardDescription>
          Cloud providers are optional, paid services billed to your own account. Photos are sent
          only after you agree for each generation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {providers.isError ? (
          <ErrorState error={providers.error} onRetry={() => void providers.refetch()} />
        ) : providers.isPending ? (
          <Skeleton className="h-40" />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {providers.data.items.map((provider) => (
              <ProviderRow key={provider.key} provider={provider} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
