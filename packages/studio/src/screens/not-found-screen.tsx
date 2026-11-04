'use client';

import { Button } from '@ai3d/ui';
import { Compass } from 'lucide-react';

import { EmptyState, PageContainer } from '../components/page';
import { useStudio } from '../context';
import { routes } from '../routes';

export function NotFoundScreen() {
  const { Link } = useStudio().navigation;
  return (
    <PageContainer>
      <EmptyState
        icon={<Compass />}
        title="Page not found"
        description="This page does not exist or was moved."
        action={
          <Button asChild variant="secondary">
            <Link href={routes.projects}>Go to projects</Link>
          </Button>
        }
      />
    </PageContainer>
  );
}
