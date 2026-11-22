"use client";

import Image from "next/image";
import { useEffect, useRef, useState } from "react";

import {
  getAutomationRun,
  searchAutomationCandidates,
  startAutomationRun,
} from "@/lib/api";
import type { AutomationCandidate, AutomationRun, AutomationSearchSort } from "@/lib/types";

type ClipAutomationProps = {
  projectId: string;
  defaultHashtag: string | null;
  onProjectChanged: () => Promise<unknown>;
};

const terminalStatuses = new Set<AutomationRun["status"]>(["completed", "failed"]);

const searchModes: { value: AutomationSearchSort; label: string }[] = [
  { value: "relevance", label: "Mais relevantes" },
  { value: "recent", label: "Últimas 24 horas" },
  { value: "views", label: "Mais visualizados (7 dias)" },
];

const statusLabels: Record<AutomationRun["status"], string> = {
  queued: "Na fila",
  downloading_source: "Baixando o vídeo original",
  downloading_references: "Preparando os cortes selecionados",
  matching: "Encontrando os trechos e gerando os cortes",
  completed: "Concluído",
  failed: "Falhou",
};

function formatDuration(value: number): string {
  const seconds = Math.round(value);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatViews(value: number): string {
  return new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 }).format(
    value,
  );
}

function CandidateCard({
  candidate,
  selected,
  onToggle,
}: {
  candidate: AutomationCandidate;
  selected: boolean;
  onToggle: (videoId: string) => void;
}) {
  return (
    <article
      className={`grid gap-4 rounded-xl border p-4 transition sm:grid-cols-[168px_1fr] ${
        selected
          ? "border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500"
          : "border-slate-200 bg-white hover:border-indigo-300"
      }`}
    >
      <div className="relative aspect-video overflow-hidden rounded-lg bg-slate-100">
        {candidate.thumbnail_url ? (
          <Image
            src={candidate.thumbnail_url}
            alt=""
            fill
            sizes="(max-width: 640px) 100vw, 168px"
            className="object-cover"
          />
        ) : null}
      </div>
      <div className="min-w-0">
        <label className="flex cursor-pointer items-start gap-3">
          <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggle(candidate.video_id)}
              className="mt-1 h-5 w-5 rounded border-slate-300 text-indigo-600"
            />
          <div className="min-w-0">
            <h3 className="font-semibold text-slate-900">{candidate.title}</h3>
            <p className="mt-1 text-sm text-slate-600">{candidate.channel_title}</p>
          </div>
        </label>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-600">
          <span>{formatViews(candidate.views)} visualizações</span>
          <span>{formatDuration(candidate.duration_seconds)}</span>
          <span>{Math.round(candidate.relevance_score * 100)}% relevante</span>
        </div>
        <a
          href={candidate.url}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-block text-sm font-semibold text-indigo-700 hover:text-indigo-800"
        >
          Conferir no YouTube ↗
        </a>
      </div>
    </article>
  );
}

export function ClipAutomation({
  projectId,
  defaultHashtag,
  onProjectChanged,
}: ClipAutomationProps) {
  const [sourceUrl, setSourceUrl] = useState("");
  const [hashtag, setHashtag] = useState(defaultHashtag?.replace(/^#/, "") ?? "");
  const [candidates, setCandidates] = useState<AutomationCandidate[]>([]);
  const [sortBy, setSortBy] = useState<AutomationSearchSort>("relevance");
  const [nextPageToken, setNextPageToken] = useState<string | null>(null);
  const [searchWindowStart, setSearchWindowStart] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [activeRun, setActiveRun] = useState<AutomationRun | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const onProjectChangedRef = useRef(onProjectChanged);

  useEffect(() => {
    onProjectChangedRef.current = onProjectChanged;
  }, [onProjectChanged]);

  useEffect(() => {
    if (!activeRun || terminalStatuses.has(activeRun.status)) return;
    const runId = activeRun.id;
    const interval = window.setInterval(() => {
      getAutomationRun(projectId, runId)
        .then((run) => {
          setActiveRun(run);
          if (terminalStatuses.has(run.status)) {
            window.clearInterval(interval);
            void onProjectChangedRef.current();
          }
        })
        .catch((requestError: unknown) => {
          window.clearInterval(interval);
          setError(
            requestError instanceof Error
              ? requestError.message
              : "Não foi possível atualizar a automação.",
          );
        });
    }, 3000);
    return () => window.clearInterval(interval);
  }, [activeRun, projectId]);

  async function handleSearch(
    requestedSort: AutomationSearchSort = sortBy,
    appendResults = false,
  ) {
    if (!sourceUrl.trim() || !hashtag.trim()) return;
    setError(null);
    if (appendResults) setIsLoadingMore(true);
    else setIsSearching(true);
    if (!appendResults) setActiveRun(null);
    try {
      const response = await searchAutomationCandidates(projectId, {
        source_url: sourceUrl.trim(),
        hashtag: hashtag.trim(),
        sort_by: requestedSort,
        page_token: appendResults ? nextPageToken : null,
        published_after: appendResults ? searchWindowStart : null,
      });
      setSortBy(requestedSort);
      setCandidates((current) => {
        if (!appendResults) return response.candidates;
        const seen = new Set(current.map((candidate) => candidate.video_id));
        return [...current, ...response.candidates.filter((candidate) => !seen.has(candidate.video_id))];
      });
      setNextPageToken(response.next_page_token);
      setSearchWindowStart(response.search_window_start);
      if (!appendResults) setSelectedIds(new Set());
      if (response.candidates.length === 0 && !appendResults) {
        setError("Nenhum corte recente e compatível foi encontrado para esta hashtag.");
      }
    } catch (requestError: unknown) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Não foi possível buscar os cortes.",
      );
    } finally {
      if (appendResults) setIsLoadingMore(false);
      else setIsSearching(false);
    }
  }

  function toggleCandidate(videoId: string) {
    if (!selectedIds.has(videoId) && selectedIds.size >= 10) {
      setError("Você pode selecionar até 10 vídeos por vez.");
      return;
    }
    setError(null);
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(videoId)) next.delete(videoId);
      else next.add(videoId);
      return next;
    });
  }

  async function handleStart() {
    if (selectedIds.size === 0) return;
    setError(null);
    setIsStarting(true);
    try {
      const run = await startAutomationRun(projectId, {
        source_url: sourceUrl.trim(),
        hashtag: hashtag.trim(),
        selected_video_ids: Array.from(selectedIds),
      });
      setActiveRun(run);
    } catch (requestError: unknown) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Não foi possível iniciar a automação.",
      );
    } finally {
      setIsStarting(false);
    }
  }

  const automationBusy = activeRun ? !terminalStatuses.has(activeRun.status) : false;

  return (
    <section className="mt-6 max-w-3xl rounded-xl border border-indigo-200 bg-gradient-to-br from-indigo-50 to-white p-6 sm:p-8">
      <p className="text-sm font-bold uppercase tracking-wider text-indigo-600">Novo fluxo automático</p>
      <h2 className="mt-2 text-2xl font-bold text-slate-900">Buscar, escolher e cortar</h2>
      <p className="mt-2 text-slate-600">
        Informe o vídeo original e a hashtag. Você escolhe os melhores cortes encontrados e o
        ClipFinder prepara as referências, localiza os trechos e gera os arquivos automaticamente.
      </p>

      <div className="mt-6 grid gap-4">
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          Link do vídeo original no YouTube
          <input
            type="url"
            value={sourceUrl}
            onChange={(event) => setSourceUrl(event.target.value)}
            placeholder="https://www.youtube.com/watch?v=..."
            className="rounded-lg border border-slate-300 bg-white px-3 py-2.5 font-normal"
          />
        </label>
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          Hashtag
          <div className="flex rounded-lg border border-slate-300 bg-white focus-within:ring-2 focus-within:ring-indigo-500">
            <span className="px-3 py-2.5 text-slate-500">#</span>
            <input
              value={hashtag}
              onChange={(event) => setHashtag(event.target.value.replace(/^#/, ""))}
              placeholder="podcast"
              className="min-w-0 flex-1 rounded-r-lg px-1 py-2.5 font-normal outline-none"
            />
          </div>
        </label>
      </div>

      <button
        type="button"
        onClick={() => void handleSearch()}
        disabled={!sourceUrl.trim() || !hashtag.trim() || isSearching || automationBusy}
        className="mt-5 rounded-lg bg-indigo-600 px-5 py-2.5 font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300"
      >
        {isSearching ? "Buscando os melhores cortes..." : "Buscar cortes relevantes"}
      </button>

      {error ? <p className="mt-5 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p> : null}

      {candidates.length > 0 ? (
        <div className="mt-7">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h3 className="text-lg font-bold text-slate-900">Escolha os cortes</h3>
              <p className="mt-1 text-sm text-slate-600">
                {selectedIds.size} de {candidates.length} selecionado(s)
              </p>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2" aria-label="Ordenar vídeos" role="group">
            {searchModes.map((mode) => (
              <button
                key={mode.value}
                type="button"
                onClick={() => void handleSearch(mode.value)}
                disabled={isSearching || isLoadingMore || automationBusy}
                aria-pressed={sortBy === mode.value}
                className={`rounded-full border px-3 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60 ${
                  sortBy === mode.value
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:border-indigo-400 hover:text-indigo-700"
                }`}
              >
                {mode.label}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            A lista mostra até 10 por busca. Você pode buscar novas páginas; cada geração aceita até 10 selecionados.
          </p>
          <div className="mt-4 space-y-3">
            {candidates.map((candidate) => (
              <CandidateCard
                key={candidate.video_id}
                candidate={candidate}
                selected={selectedIds.has(candidate.video_id)}
                onToggle={toggleCandidate}
              />
            ))}
          </div>
          {nextPageToken ? (
            <button
              type="button"
              onClick={() => void handleSearch(sortBy, true)}
              disabled={isSearching || isLoadingMore || automationBusy}
              className="mt-4 rounded-lg border border-indigo-300 bg-white px-4 py-2.5 font-semibold text-indigo-700 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isLoadingMore ? "Buscando mais vídeos..." : "Atualizar e buscar mais vídeos"}
            </button>
          ) : null}
          <button
            type="button"
            onClick={handleStart}
            disabled={selectedIds.size === 0 || isStarting || automationBusy || isSearching || isLoadingMore}
            className="mt-5 rounded-lg bg-emerald-600 px-5 py-2.5 font-semibold text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
          >
            {isStarting ? "Iniciando..." : `Gerar ${selectedIds.size} corte(s) selecionado(s)`}
          </button>
          <p className="mt-3 text-xs text-slate-500">
            Use apenas vídeos que você tem autorização para baixar e reutilizar.
          </p>
        </div>
      ) : null}

      {activeRun ? (
        <div
          className={`mt-6 rounded-xl border p-4 ${
            activeRun.status === "failed"
              ? "border-red-200 bg-red-50"
              : activeRun.status === "completed"
                ? "border-emerald-200 bg-emerald-50"
                : "border-indigo-200 bg-white"
          }`}
          aria-live="polite"
        >
          <p className="font-bold text-slate-900">{statusLabels[activeRun.status]}</p>
          <p className="mt-1 text-sm text-slate-600">
            Referências preparadas: {activeRun.ready_references}/{activeRun.total_references}
          </p>
          {activeRun.status === "matching" ? (
            <p className="mt-1 text-sm text-slate-600">
              {activeRun.jobs_started} corte(s) em processamento.
            </p>
          ) : null}
          {activeRun.error_message ? (
            <p className="mt-3 text-sm text-red-700">{activeRun.error_message}</p>
          ) : null}
          {activeRun.status === "completed" ? (
            <p className="mt-3 text-sm font-medium text-emerald-800">
              Os resultados apareceram na seção “Matching e cortes”.
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
