import type {
  GeneratedClip,
  Project,
  ProjectDetail,
  ProjectInput,
  ReferenceVideo,
  SourceVideo,
  YoutubeSearchResult,
} from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, options);

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? "Não foi possível concluir a solicitação.");
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export function getProjects(): Promise<Project[]> {
  return request<Project[]>("/projects");
}

export function getProject(projectId: string): Promise<ProjectDetail> {
  return request<ProjectDetail>(`/projects/${projectId}`);
}

export function createProject(payload: ProjectInput): Promise<Project> {
  return request<Project>("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteProject(projectId: string): Promise<void> {
  return request<void>(`/projects/${projectId}`, { method: "DELETE" });
}

export function uploadSourceVideo(projectId: string, file: File): Promise<SourceVideo> {
  const formData = new FormData();
  formData.append("file", file);

  return request<SourceVideo>(`/projects/${projectId}/upload`, {
    method: "POST",
    body: formData,
  });
}

export function uploadReferenceVideo(projectId: string, file: File): Promise<ReferenceVideo> {
  const formData = new FormData();
  formData.append("file", file);

  return request<ReferenceVideo>(`/projects/${projectId}/reference-videos/upload`, {
    method: "POST",
    body: formData,
  });
}

export function searchYoutube(projectId: string, query: string): Promise<YoutubeSearchResult[]> {
  return request<YoutubeSearchResult[]>(`/projects/${projectId}/youtube/search?query=${encodeURIComponent(query)}`);
}

export function addYoutubeReference(
  projectId: string,
  result: YoutubeSearchResult,
): Promise<ReferenceVideo> {
  return request<ReferenceVideo>(`/projects/${projectId}/reference-videos/youtube`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_id: result.video_id,
      title: result.title,
      url: result.url,
      channel_title: result.channel_title,
      thumbnail_url: result.thumbnail_url,
      description: result.description,
    }),
  });
}

export function generateClips(projectId: string): Promise<{ message: string; jobs_started: number }> {
  return request(`/projects/${projectId}/generated-clips`, { method: "POST" });
}

export function generatedClipDownloadUrl(projectId: string, clipId: string): string {
  return `${API_URL}/projects/${projectId}/generated-clips/${clipId}/download`;
}
