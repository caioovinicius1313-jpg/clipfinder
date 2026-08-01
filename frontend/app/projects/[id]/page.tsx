import { ProjectDetails } from "@/components/project-details";

type ProjectPageProps = {
  params: { id: string };
};

export default function ProjectPage({ params }: ProjectPageProps) {
  return <ProjectDetails projectId={params.id} />;
}
