"use client";

import Link from "next/link";
import { ChangeEvent, useEffect, useState } from "react";

import { AppShell } from "@/components/app-shell";
import {
  addYoutubeReference,
  generateClips,
  generatedClipDownloadUrl,
  getProject,
  searchYoutube,
  uploadReferenceVideo,
  uploadSourceVideo,
} from "@/lib/api";
import type {
  GeneratedClip,
  ProjectDetail,
  ReferenceVideo,
  SourceVideo,
  YoutubeSearchResult,
} from "@/lib/types";

type ProjectDetailsProps = {
  projectId: string;
};

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB"];
  let value = size / 1024;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function formatDuration(duration: number | null): string {
  if (duration === null) return "Ainda não disponível";
  const totalSeconds = Math.round(duration);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function formatTimestamp(value: number | null): string {
  if (value === null) return "—";
  return formatDuration(value);
}

function formatStatus(status: string): string {
  const labels: Record<string, string> = {
    uploaded: "Enviado",
    reference_only: "Apenas referência",
    processing: "Processando",
    completed: "Concluído",
    failed: "Não encontrado",
  };
  return labels[status] ?? status;
}

function VideoInfo({ video }: { video: Pick<SourceVideo, "filename" | "size" | "status" | "duration"> }) {
  return (
    <dl className="grid gap-4 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm sm:grid-cols-4">
      <div>
        <dt className="text-slate-500">Nome</dt>
        <dd className="mt-1 break-words font-medium text-slate-900">{video.filename}</dd>
      </div>
      <div>
        <dt className="text-slate-500">Tamanho</dt>
        <dd className="mt-1 font-medium text-slate-900">{formatFileSize(video.size)}</dd>
      </div>
      <div>
        <dt className="text-slate-500">Duração</dt>
        <dd className="mt-1 font-medium text-slate-900">{formatDuration(video.duration)}</dd>
      </div>
      <div>
        <dt className="text-slate-500">Status</dt>
        <dd className="mt-1 font-medium text-slate-900">{formatStatus(video.status)}</dd>
      </div>
    </dl>
  );
}

function ReferenceCard({ reference }: { reference: ReferenceVideo }) {
  return (
    <article className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="font-semibold text-slate-900">{reference.title}</h3>
          {reference.channel_title && <p className="mt-1 text-slate-600">{reference.channel_title}</p>}
        </div>
        <span className="font-medium text-indigo-700">{formatStatus(reference.status)}</span>
      </div>
      <p className="mt-3 text-slate-600">
        {reference.source === "upload"
          ? `${formatFileSize(reference.size ?? 0)} · ${formatDuration(reference.duration)}`
          : "Link salvo como referência. Envie o arquivo do corte para usá-lo no matching."}
      </p>
      {reference.external_url && (
        <a
          href={reference.external_url}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-block font-medium text-indigo-700 hover:text-indigo-800"
        >
          Abrir no YouTube ↗
        </a>
      )}
    </article>
  );
}

function GeneratedClipCard({ clip, projectId }: { clip: GeneratedClip; projectId: string }) {
  return (
    <article className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-slate-900">Corte encontrado</h3>
          <p className="mt-1 text-slate-600">
            Original: {formatTimestamp(clip.original_start_seconds)} até {formatTimestamp(clip.original_end_seconds)}
          </p>
        </div>
        <span className="font-medium text-indigo-700">{formatStatus(clip.status)}</span>
      </div>
      {clip.match_score !== null && (
        <p className="mt-3 text-slate-600">Confiança do áudio: {Math.round(clip.match_score * 100)}%</p>
      )}
      {clip.error_message && <p className="mt-3 text-red-700">{clip.error_message}</p>}
      {clip.status === "completed" && (
        <a
          href={generatedClipDownloadUrl(projectId, clip.id)}
          className="mt-4 inline-block rounded-lg bg-indigo-600 px-4 py-2 font-semibold text-white hover:bg-indigo-700"
        >
          Baixar corte
        </a>
      )}
    </article>
  );
}

export function ProjectDetails({ projectId }: ProjectDetailsProps) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [selectedSourceFile, setSelectedSourceFile] = useState<File | null>(null);
  const [selectedReferenceFile, setSelectedReferenceFile] = useState<File | null>(null);
  const [youtubeQuery, setYoutubeQuery] = useState("");
  const [youtubeResults, setYoutubeResults] = useState<YoutubeSearchResult[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploadingSource, setIsUploadingSource] = useState(false);
  const [isUploadingReference, setIsUploadingReference] = useState(false);
  const [sourceUploadProgress, setSourceUploadProgress] = useState(0);
  const [referenceUploadProgress, setReferenceUploadProgress] = useState(0);
  const [isSearchingYoutube, setIsSearchingYoutube] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [importingYoutubeId, setImportingYoutubeId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loadProject() {
    const loadedProject = await getProject(projectId);
    setProject(loadedProject);
    return loadedProject;
  }

  useEffect(() => {
    loadProject()
      .then((loadedProject) => setYoutubeQuery(loadedProject.hashtag || loadedProject.name))
      .catch((requestError: unknown) => {
        setError(requestError instanceof Error ? requestError.message : "Não foi possível carregar o projeto.");
      })
      .finally(() => setIsLoading(false));
  }, [projectId]);

  function clearMessages() {
    setError(null);
    setNotice(null);
  }

  async function handleSourceUpload() {
    if (!selectedSourceFile) return;
    clearMessages();
    setIsUploadingSource(true);
    setSourceUploadProgress(0);
    try {
      await uploadSourceVideo(projectId, selectedSourceFile, setSourceUploadProgress);
      await loadProject();
      setSelectedSourceFile(null);
      setNotice("Vídeo original enviado.");
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível enviar o vídeo.");
    } finally {
      setIsUploadingSource(false);
      setSourceUploadProgress(0);
    }
  }

  async function handleReferenceUpload() {
    if (!selectedReferenceFile) return;
    clearMessages();
    setIsUploadingReference(true);
    setReferenceUploadProgress(0);
    try {
      await uploadReferenceVideo(projectId, selectedReferenceFile, setReferenceUploadProgress);
      await loadProject();
      setSelectedReferenceFile(null);
      setNotice("Vídeo de referência enviado.");
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível enviar a referência.");
    } finally {
      setIsUploadingReference(false);
      setReferenceUploadProgress(0);
    }
  }

  async function handleYoutubeSearch() {
    if (!youtubeQuery.trim()) return;
    clearMessages();
    setIsSearchingYoutube(true);
    try {
      setYoutubeResults(await searchYoutube(projectId, youtubeQuery.trim()));
      setNotice("Resultados do YouTube carregados. Salve links ou envie os arquivos dos cortes para o matching.");
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível pesquisar no YouTube.");
    } finally {
      setIsSearchingYoutube(false);
    }
  }

  async function handleYoutubeReference(result: YoutubeSearchResult) {
    clearMessages();
    setImportingYoutubeId(result.video_id);
    try {
      await addYoutubeReference(projectId, result);
      await loadProject();
      setNotice("Link do YouTube salvo como referência.");
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível salvar a referência.");
    } finally {
      setImportingYoutubeId(null);
    }
  }

  async function handleGenerateClips() {
    clearMessages();
    setIsGenerating(true);
    try {
      const response = await generateClips(projectId);
      await loadProject();
      setNotice(`${response.jobs_started} matching(s) iniciado(s). Clique em “Atualizar status” para acompanhar.`);
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível iniciar o matching.");
    } finally {
      setIsGenerating(false);
    }
  }

  if (isLoading) return <AppShell><p className="text-slate-600">Carregando projeto...</p></AppShell>;
  if (error && !project) return <AppShell><p className="rounded-lg bg-red-50 p-4 text-red-700">{error}</p></AppShell>;
  if (!project) return <AppShell><p className="text-slate-600">Projeto não encontrado.</p></AppShell>;

  const latestSourceVideo = project.source_videos.at(-1);
  const uploadedReferences = project.reference_videos.filter((reference) => reference.source === "upload");

  return (
    <AppShell>
      <Link href="/" className="text-sm font-medium text-indigo-700 hover:text-indigo-800">← Voltar para projetos</Link>

      <section className="mt-5 border-b border-slate-200 pb-7">
        <p className="text-sm font-semibold uppercase tracking-wider text-indigo-600">Projeto</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-slate-900">{project.name}</h1>
        <p className="mt-2 max-w-2xl text-slate-600">{project.description || "Sem descrição"}</p>
        {project.hashtag && <p className="mt-3 font-medium text-indigo-700">{project.hashtag}</p>}
      </section>

      {(notice || error) && (
        <div className="mt-6 space-y-3">
          {notice && <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
          {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        </div>
      )}

      <section className="mt-8 max-w-3xl rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
        <h2 className="text-xl font-bold text-slate-900">Vídeo original</h2>
        <p className="mt-2 text-slate-600">Envie o vídeo completo que será usado para gerar os cortes.</p>
        <input type="file" accept="video/*" onChange={(event) => setSelectedSourceFile(event.target.files?.[0] ?? null)} className="mt-6 block w-full cursor-pointer rounded-lg border border-slate-300 p-2 text-sm text-slate-700" />
        <div className="mt-5 space-y-3">
          {selectedSourceFile && <VideoInfo video={{ filename: selectedSourceFile.name, size: selectedSourceFile.size, duration: null, status: "Pronto para envio" }} />}
          {!selectedSourceFile && latestSourceVideo && <VideoInfo video={latestSourceVideo} />}
        </div>
        <button type="button" onClick={handleSourceUpload} disabled={!selectedSourceFile || isUploadingSource} className="mt-6 rounded-lg bg-indigo-600 px-5 py-2.5 font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300">
          {isUploadingSource ? `Enviando ${sourceUploadProgress}%` : "Enviar vídeo original"}
        </button>
        {isUploadingSource && (
          <div className="mt-4" aria-label={`Upload do vídeo original: ${sourceUploadProgress}%`}>
            <div className="h-2 overflow-hidden rounded-full bg-slate-200">
              <div className="h-full rounded-full bg-indigo-600 transition-all" style={{ width: `${sourceUploadProgress}%` }} />
            </div>
            <p className="mt-2 text-sm text-slate-600">Não feche esta página durante o envio.</p>
          </div>
        )}
      </section>

      <section className="mt-6 max-w-3xl rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
        <h2 className="text-xl font-bold text-slate-900">Cortes de referência</h2>
        <p className="mt-2 text-slate-600">Envie os arquivos dos cortes que devem ser encontrados dentro do vídeo original.</p>
        <input type="file" accept="video/*" onChange={(event) => setSelectedReferenceFile(event.target.files?.[0] ?? null)} className="mt-6 block w-full cursor-pointer rounded-lg border border-slate-300 p-2 text-sm text-slate-700" />
        {selectedReferenceFile && <p className="mt-3 text-sm text-slate-700">Selecionado: {selectedReferenceFile.name}</p>}
        <button type="button" onClick={handleReferenceUpload} disabled={!selectedReferenceFile || isUploadingReference} className="mt-5 rounded-lg bg-indigo-600 px-5 py-2.5 font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300">
          {isUploadingReference ? `Enviando ${referenceUploadProgress}%` : "Enviar corte de referência"}
        </button>
        {isUploadingReference && (
          <div className="mt-4" aria-label={`Upload da referência: ${referenceUploadProgress}%`}>
            <div className="h-2 overflow-hidden rounded-full bg-slate-200">
              <div className="h-full rounded-full bg-indigo-600 transition-all" style={{ width: `${referenceUploadProgress}%` }} />
            </div>
          </div>
        )}
        {uploadedReferences.length > 0 && <div className="mt-6 space-y-3">{uploadedReferences.map((reference) => <ReferenceCard key={reference.id} reference={reference} />)}</div>}
      </section>

      <section className="mt-6 max-w-3xl rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
        <h2 className="text-xl font-bold text-slate-900">Pesquisar no YouTube</h2>
        <p className="mt-2 text-slate-600">Use os resultados para organizar links de referência. Para gerar cortes, envie o arquivo autorizado correspondente acima.</p>
        <div className="mt-5 flex flex-col gap-3 sm:flex-row">
          <input value={youtubeQuery} onChange={(event) => setYoutubeQuery(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2.5" placeholder="Pesquise por tema ou hashtag" />
          <button type="button" onClick={handleYoutubeSearch} disabled={!youtubeQuery.trim() || isSearchingYoutube} className="rounded-lg border border-indigo-600 px-5 py-2.5 font-semibold text-indigo-700 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:border-slate-300 disabled:text-slate-400">
            {isSearchingYoutube ? "Buscando..." : "Buscar"}
          </button>
        </div>
        {youtubeResults.length > 0 && <div className="mt-6 space-y-3">{youtubeResults.map((result) => <article key={result.video_id} className="rounded-lg border border-slate-200 p-4"><h3 className="font-semibold text-slate-900">{result.title}</h3><p className="mt-1 text-sm text-slate-600">{result.channel_title}</p><div className="mt-4 flex flex-wrap gap-3"><a href={result.url} target="_blank" rel="noreferrer" className="font-medium text-indigo-700">Abrir no YouTube ↗</a><button type="button" onClick={() => handleYoutubeReference(result)} disabled={importingYoutubeId === result.video_id} className="font-semibold text-indigo-700 disabled:text-indigo-300">{importingYoutubeId === result.video_id ? "Salvando..." : "Salvar referência"}</button></div></article>)}</div>}
      </section>

      <section className="mt-6 max-w-3xl rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div><h2 className="text-xl font-bold text-slate-900">Matching e cortes</h2><p className="mt-2 text-slate-600">Compare os áudios enviados e gere cortes novos a partir do vídeo original.</p></div>
          <div className="flex gap-3"><button type="button" onClick={() => { clearMessages(); loadProject().catch(() => setError("Não foi possível atualizar o status.")); }} className="rounded-lg border border-slate-300 px-4 py-2 font-semibold text-slate-700 hover:bg-slate-50">Atualizar status</button><button type="button" onClick={handleGenerateClips} disabled={isGenerating || !latestSourceVideo || uploadedReferences.length === 0} className="rounded-lg bg-indigo-600 px-4 py-2 font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300">{isGenerating ? "Iniciando..." : "Encontrar e gerar cortes"}</button></div>
        </div>
        {project.generated_clips.length === 0 && <p className="mt-6 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">Nenhum corte gerado ainda.</p>}
        {project.generated_clips.length > 0 && <div className="mt-6 space-y-3">{project.generated_clips.map((clip) => <GeneratedClipCard key={clip.id} clip={clip} projectId={projectId} />)}</div>}
      </section>
    </AppShell>
  );
}
