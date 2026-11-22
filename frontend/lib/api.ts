import type {
  AutomationRun,
  AutomationSearchResponse,
  AutomationSearchSort,
  GeneratedClip,
  HashtagRanking,
  HashtagWeeklyRanking,
  Project,
  ProjectDetail,
  ProjectInput,
  ReferenceVideo,
  SourceVideo,
  YoutubeSearchResult,
  YoutubeRankingMetric,
} from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, options);

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ApiRequestError(
      payload?.detail ?? "Não foi possível concluir a solicitação.",
      response.status,
    );
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

type UploadKind = "source" | "reference";

type MultipartUploadSession = {
  upload_id: string;
  object_key: string;
  part_size: number;
  part_count: number;
};

type CompletedPart = {
  part_number: number;
  etag: string;
};

async function legacyUpload<T>(
  path: string,
  file: File,
  onProgress?: (percentage: number) => void,
): Promise<T> {
  const formData = new FormData();
  formData.append("file", file);
  onProgress?.(0);
  const result = await request<T>(path, {
    method: "POST",
    body: formData,
  });
  onProgress?.(100);
  return result;
}

async function multipartUpload<T>(
  projectId: string,
  file: File,
  kind: UploadKind,
  onProgress?: (percentage: number) => void,
): Promise<T> {
  let session: MultipartUploadSession | null = null;
  const contentType = file.type || "application/octet-stream";

  try {
    session = await request<MultipartUploadSession>(`/projects/${projectId}/uploads/multipart`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        content_type: contentType,
        size: file.size,
        kind,
      }),
    });
    const activeSession = session;

    const completedParts: CompletedPart[] = [];
    let uploadedBytes = 0;
    let nextPartIndex = 0;
    onProgress?.(0);

    const uploadNextPart = async (): Promise<void> => {
      while (true) {
        const currentIndex = nextPartIndex;
        nextPartIndex += 1;
        if (currentIndex >= activeSession.part_count) return;

        const partNumber = currentIndex + 1;
        const start = currentIndex * activeSession.part_size;
        const end = Math.min(start + activeSession.part_size, file.size);
        const part = file.slice(start, end);
        const { url } = await request<{ url: string }>(
          `/projects/${projectId}/uploads/multipart/part-url`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              upload_id: activeSession.upload_id,
              object_key: activeSession.object_key,
              part_number: partNumber,
            }),
          },
        );

        const uploadResponse = await fetch(url, { method: "PUT", body: part });
        if (!uploadResponse.ok) {
          throw new Error(`O Bucket recusou a parte ${partNumber} do arquivo.`);
        }
        const etag = uploadResponse.headers.get("etag");
        if (!etag) {
          throw new Error(
            "O Bucket não expôs o identificador ETag. Verifique a configuração CORS do Bucket.",
          );
        }

        completedParts.push({ part_number: partNumber, etag });
        uploadedBytes += part.size;
        onProgress?.(Math.min(99, Math.round((uploadedBytes / file.size) * 100)));
      }
    };

    const workers = Math.min(3, activeSession.part_count);
    await Promise.all(Array.from({ length: workers }, () => uploadNextPart()));

    const result = await request<T>(`/projects/${projectId}/uploads/multipart/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: activeSession.upload_id,
        object_key: activeSession.object_key,
        filename: file.name,
        content_type: contentType,
        size: file.size,
        kind,
        parts: completedParts.sort((left, right) => left.part_number - right.part_number),
      }),
    });
    onProgress?.(100);
    return result;
  } catch (error) {
    if (session) {
      await request<void>(`/projects/${projectId}/uploads/multipart/abort`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          upload_id: session.upload_id,
          object_key: session.object_key,
          kind,
        }),
      }).catch(() => undefined);
    }
    throw error;
  }
}

export async function uploadSourceVideo(
  projectId: string,
  file: File,
  onProgress?: (percentage: number) => void,
): Promise<SourceVideo> {
  try {
    return await multipartUpload<SourceVideo>(projectId, file, "source", onProgress);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 503) {
      return legacyUpload<SourceVideo>(`/projects/${projectId}/upload`, file, onProgress);
    }
    throw error;
  }
}

export async function uploadReferenceVideo(
  projectId: string,
  file: File,
  onProgress?: (percentage: number) => void,
): Promise<ReferenceVideo> {
  try {
    return await multipartUpload<ReferenceVideo>(projectId, file, "reference", onProgress);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 503) {
      return legacyUpload<ReferenceVideo>(
        `/projects/${projectId}/reference-videos/upload`,
        file,
        onProgress,
      );
    }
    throw error;
  }
}

export function searchYoutube(projectId: string, query: string): Promise<YoutubeSearchResult[]> {
  return request<YoutubeSearchResult[]>(`/projects/${projectId}/youtube/search?query=${encodeURIComponent(query)}`);
}

export function getHashtagRanking(
  projectId: string,
  metric: YoutubeRankingMetric,
): Promise<HashtagRanking> {
  return request<HashtagRanking>(
    `/projects/${projectId}/youtube/ranking?metric=${encodeURIComponent(metric)}`,
  );
}

export function hashtagRankingDownloadUrl(projectId: string, statId: string): string {
  return `${API_URL}/projects/${projectId}/youtube/ranking/${statId}/download`;
}

export function refreshHashtagRanking(projectId: string): Promise<HashtagRanking> {
  return request<HashtagRanking>(`/projects/${projectId}/youtube/ranking/refresh`, {
    method: "POST",
  });
}

export function getWeeklyHashtagRanking(projectId: string): Promise<HashtagWeeklyRanking> {
  return request<HashtagWeeklyRanking>(`/projects/${projectId}/youtube/ranking/weekly`);
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

export function deleteReferenceVideo(
  projectId: string,
  referenceVideoId: string,
): Promise<void> {
  return request<void>(`/projects/${projectId}/reference-videos/${referenceVideoId}`, {
    method: "DELETE",
  });
}

export function generateClips(projectId: string): Promise<{ message: string; jobs_started: number }> {
  return request(`/projects/${projectId}/generated-clips`, { method: "POST" });
}

export function generatedClipDownloadUrl(projectId: string, clipId: string): string {
  return `${API_URL}/projects/${projectId}/generated-clips/${clipId}/download`;
}

export function searchAutomationCandidates(
  projectId: string,
  payload: {
    source_url: string;
    hashtag: string;
    sort_by: AutomationSearchSort;
    page_token?: string | null;
    published_after?: string | null;
  },
): Promise<AutomationSearchResponse> {
  return request<AutomationSearchResponse>(`/projects/${projectId}/automation/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function startAutomationRun(
  projectId: string,
  payload: { source_url: string; hashtag: string; selected_video_ids: string[] },
): Promise<AutomationRun> {
  return request<AutomationRun>(`/projects/${projectId}/automation/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getAutomationRun(projectId: string, runId: string): Promise<AutomationRun> {
  return request<AutomationRun>(`/projects/${projectId}/automation/runs/${runId}`);
}
