'use client';

import type { ProjectRead, QualityMode, SceneRenderAgain } from '@ai3d/shared-types';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  FieldHint,
  Label,
  Textarea,
} from '@ai3d/ui';
import { AlertTriangle, Clapperboard, Server } from 'lucide-react';
import { useId, useRef, useState, type ReactNode } from 'react';

import { newIdempotencyKey } from '../generation';
import { useCreateScene, useProviders, useSceneReadiness } from '../api/hooks';
import { useStudio } from '../context';
import {
  qualityModeLabel,
  sceneAngleLabel,
  sceneGroundLabel,
  sceneLightingLabel,
  sceneShotLabel,
  sceneStyleLabel,
} from '../labels';
import { routes } from '../routes';
import { ErrorState } from './page';

export const selectClass =
  'h-9 w-full rounded-md border border-border-strong bg-surface px-2.5 text-[13px] text-foreground focus-visible:outline-2 focus-visible:outline-accent';

export const SCENE_SECONDS = [4, 6, 8, 10, 15] as const;

export type Choices = Required<Omit<SceneRenderAgain, 'quality_mode'>> & {
  quality_mode: QualityMode;
};

export function defaultChoices(quality: QualityMode): Choices {
  return {
    seconds: 6,
    quality_mode: quality,
    shot: 'auto',
    angle: 'auto',
    lighting: 'auto',
    ground: 'auto',
    style: 'auto',
  };
}

function Choice({
  label,
  value,
  options,
  onChange,
  auto = true,
}: {
  label: string;
  value: string;
  options: Record<string, string>;
  onChange: (value: string) => void;
  auto?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      <select
        id={id}
        className={selectClass}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {auto ? <option value="auto">Let the AI choose</option> : null}
        {Object.entries(options).map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The shot settings shared by "Make a scene" and "Render again". */
export function ChoiceFields({
  choices,
  onChange,
}: {
  choices: Choices;
  onChange: (choices: Choices) => void;
}) {
  const set = (field: keyof Choices) => (value: string) =>
    onChange({ ...choices, [field]: field === 'seconds' ? Number(value) : value });
  return (
    <div className="grid grid-cols-2 gap-3">
      <Choice
        label="Length"
        value={String(choices.seconds)}
        options={Object.fromEntries(SCENE_SECONDS.map((s) => [String(s), `${s} seconds`]))}
        onChange={set('seconds')}
        auto={false}
      />
      <Choice
        label="Quality"
        value={choices.quality_mode}
        options={qualityModeLabel}
        onChange={set('quality_mode')}
        auto={false}
      />
      <Choice
        label="Camera move"
        value={choices.shot}
        options={sceneShotLabel}
        onChange={set('shot')}
      />
      <Choice
        label="Camera angle"
        value={choices.angle}
        options={sceneAngleLabel}
        onChange={set('angle')}
      />
      <Choice
        label="Light"
        value={choices.lighting}
        options={sceneLightingLabel}
        onChange={set('lighting')}
      />
      <Choice
        label="Set"
        value={choices.ground}
        options={sceneGroundLabel}
        onChange={set('ground')}
      />
      <Choice
        label="Look"
        value={choices.style}
        options={sceneStyleLabel}
        onChange={set('style')}
      />
    </div>
  );
}

function NotReady({ problems }: { problems: string[] }) {
  const { Link } = useStudio().navigation;
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border-strong p-3 text-xs leading-relaxed">
      <p className="flex items-center gap-1.5 font-medium">
        <AlertTriangle className="size-3.5 text-warning" aria-hidden /> Before you can make scenes
      </p>
      <ul className="flex list-disc flex-col gap-1 pl-4 text-muted-foreground">
        {problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
      <Link href={routes.settings} className="text-accent hover:underline">
        Open Settings
      </Link>
    </div>
  );
}

export function ScenePanel({ project }: { project: ProjectRead }) {
  const ids = { prompt: useId() };
  const readiness = useSceneReadiness();
  const providers = useProviders();
  const create = useCreateScene(project.id);
  const [prompt, setPrompt] = useState('');
  const [choices, setChoices] = useState<Choices>(() =>
    defaultChoices(project.default_quality_mode),
  );
  const [consent, setConsent] = useState(false);
  const idempotencyKey = useRef(newIdempotencyKey());
  const server = providers.data?.items.find((p) => p.key === 'gpu-server');
  const ready = readiness.data?.ready === true;
  const usable = prompt.trim().length >= 3;

  function make() {
    create.mutate(
      {
        body: { ...choices, prompt: prompt.trim(), send_to_gpu_server: consent },
        idempotencyKey: idempotencyKey.current,
      },
      {
        onSuccess: () => {
          idempotencyKey.current = newIdempotencyKey();
          setPrompt('');
          setConsent(false);
        },
      },
    );
  }

  let body: ReactNode;
  if (readiness.isError) {
    body = <ErrorState error={readiness.error} onRetry={() => void readiness.refetch()} />;
  } else if (readiness.data && !ready) {
    body = <NotReady problems={readiness.data.problems} />;
  } else {
    body = null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Clapperboard className="size-4 text-accent" aria-hidden /> Make a scene
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {body}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={ids.prompt}>Describe the shot</Label>
          <Textarea
            id={ids.prompt}
            value={prompt}
            maxLength={2000}
            rows={4}
            placeholder="A red vintage car parked next to a palm tree on a beach at sunset, the camera slowly circling it"
            onChange={(event) => setPrompt(event.target.value)}
          />
          <FieldHint>
            Name the objects, where they are and what they do. Up to six objects; each becomes a 3D
            model in this project.
          </FieldHint>
        </div>
        <ChoiceFields choices={choices} onChange={setChoices} />
        <label className="flex gap-2.5 rounded-md border border-border-strong p-3 text-xs leading-relaxed">
          <input
            type="checkbox"
            className="mt-0.5 size-4 shrink-0 accent-accent"
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
          />
          <span>
            <Server className="mr-1 inline size-3.5 text-accent" aria-hidden />
            Send this prompt to your GPU server
            {server?.base_url ? (
              <>
                {' '}
                (<code className="font-mono">{server.base_url}</code>)
              </>
            ) : null}
            . It plans the scene and makes each object there; nothing else is contacted. The shot is
            rendered on this computer with Blender.
          </span>
        </label>
        {create.isError ? <ErrorState error={create.error} /> : null}
        <Button
          variant="primary"
          disabled={!ready || !usable || !consent || create.isPending}
          onClick={make}
        >
          <Clapperboard aria-hidden /> Make scene
        </Button>
      </CardContent>
    </Card>
  );
}
