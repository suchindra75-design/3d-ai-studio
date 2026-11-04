'use client';

import type { InputMode, ProcessingMode, QualityMode } from '@ai3d/shared-types';
import {
  Button,
  Card,
  CardContent,
  ChoiceCards,
  FieldHint,
  Input,
  Label,
  Switch,
  Textarea,
} from '@ai3d/ui';
import { Camera, Images } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';

import { useCreateProject } from '../api/hooks';
import { ErrorState, PageContainer, PageHeader } from '../components/page';
import { useStudio } from '../context';
import { routes } from '../routes';

const INPUT_MODES = [
  {
    value: 'single_image' as const,
    label: 'Single photo',
    description:
      'Fast AI generation from one image. Visible surfaces follow the photo; hidden sides are invented by the model.',
    icon: <Camera />,
  },
  {
    value: 'multi_view' as const,
    label: 'Multiple photos',
    description:
      'Photogrammetry from 20+ overlapping photos for measured geometry, or AI multi-view from 2–4 angles.',
    icon: <Images />,
  },
];

const QUALITY_MODES = [
  { value: 'fast' as const, label: 'Fast', description: 'Quick previews and prototypes.' },
  {
    value: 'high_quality' as const,
    label: 'High Quality',
    description: 'Detailed geometry and PBR textures where supported.',
  },
  {
    value: 'professional' as const,
    label: 'Professional',
    description: 'Validation, cleanup and a detailed quality report.',
  },
];

const PROCESSING_MODES = [
  {
    value: 'auto' as const,
    label: 'Automatic',
    description: 'Best available route. Asks before uploading anything.',
  },
  { value: 'local' as const, label: 'Local only', description: 'Photos never leave this machine.' },
  { value: 'cloud' as const, label: 'Cloud', description: 'Use configured AI providers.' },
];

export function NewProjectScreen() {
  const { navigation } = useStudio();
  const create = useCreateProject();
  const ids = {
    name: useId(),
    description: useId(),
    input: useId(),
    quality: useId(),
    processing: useId(),
    cloud: useId(),
  };

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [inputMode, setInputMode] = useState<InputMode>('single_image');
  const [quality, setQuality] = useState<QualityMode>('fast');
  const [processing, setProcessing] = useState<ProcessingMode>('auto');
  const [allowCloud, setAllowCloud] = useState(false);
  const [touched, setTouched] = useState(false);

  const nameError = touched && !name.trim() ? 'Give the project a name.' : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!name.trim()) return;
    create.mutate(
      {
        name: name.trim(),
        description: description.trim(),
        input_mode: inputMode,
        default_quality_mode: quality,
        default_processing_mode: processing,
        allow_cloud_upload: processing === 'local' ? false : allowCloud,
      },
      { onSuccess: (project) => navigation.replace(routes.project(project.id)) },
    );
  }

  return (
    <PageContainer className="max-w-3xl">
      <PageHeader
        title="New project"
        description="Settings can be changed later; they are defaults for future generations."
      />
      <form onSubmit={submit} noValidate className="flex flex-col gap-6">
        <Card>
          <CardContent className="flex flex-col gap-4 pt-5">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.name}>Name</Label>
              <Input
                id={ids.name}
                value={name}
                maxLength={200}
                autoFocus
                placeholder="e.g. Ceramic mug"
                aria-invalid={nameError ? true : undefined}
                aria-describedby={nameError ? `${ids.name}-error` : undefined}
                onChange={(event) => setName(event.target.value)}
                onBlur={() => setTouched(true)}
              />
              {nameError ? (
                <p id={`${ids.name}-error`} className="text-xs text-danger">
                  {nameError}
                </p>
              ) : null}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.description}>
                Description <span className="font-normal text-subtle-foreground">(optional)</span>
              </Label>
              <Textarea
                id={ids.description}
                value={description}
                maxLength={5000}
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
          </CardContent>
        </Card>

        <section className="flex flex-col gap-2">
          <h2 id={ids.input} className="text-sm font-medium">
            Input
          </h2>
          <ChoiceCards
            name="input_mode"
            aria-labelledby={ids.input}
            value={inputMode}
            onValueChange={setInputMode}
            options={INPUT_MODES}
            className="sm:grid-cols-2"
          />
        </section>

        <section className="flex flex-col gap-2">
          <h2 id={ids.quality} className="text-sm font-medium">
            Default quality
          </h2>
          <ChoiceCards
            name="quality_mode"
            aria-labelledby={ids.quality}
            value={quality}
            onValueChange={setQuality}
            options={QUALITY_MODES}
            className="sm:grid-cols-3"
          />
        </section>

        <section className="flex flex-col gap-2">
          <h2 id={ids.processing} className="text-sm font-medium">
            Processing
          </h2>
          <ChoiceCards
            name="processing_mode"
            aria-labelledby={ids.processing}
            value={processing}
            onValueChange={setProcessing}
            options={PROCESSING_MODES}
            className="sm:grid-cols-3"
          />
          <div className="mt-2 flex items-start gap-3 rounded-md border border-border bg-surface p-3.5">
            <Switch
              id={ids.cloud}
              checked={processing !== 'local' && allowCloud}
              disabled={processing === 'local'}
              onCheckedChange={setAllowCloud}
              aria-describedby={`${ids.cloud}-hint`}
            />
            <div className="flex flex-col gap-0.5">
              <Label htmlFor={ids.cloud}>
                Allow sending this project’s photos to cloud providers
              </Label>
              <FieldHint id={`${ids.cloud}-hint`}>
                Off by default. When off, photos are processed only on this machine. Location data
                is always stripped from copies sent to a provider.
              </FieldHint>
            </div>
          </div>
        </section>

        {create.isError ? <ErrorState error={create.error} /> : null}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => navigation.push(routes.projects)}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create project'}
          </Button>
        </div>
      </form>
    </PageContainer>
  );
}
