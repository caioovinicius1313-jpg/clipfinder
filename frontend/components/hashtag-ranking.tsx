"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

import {
  getHashtagRanking,
  getWeeklyHashtagRanking,
  hashtagRankingDownloadUrl,
  refreshHashtagRanking,
} from "@/lib/api";
import type {
  HashtagRanking,
  HashtagRankingVideo,
  HashtagWeeklyRanking,
  YoutubeRankingMetric,
} from "@/lib/types";

type HashtagRankingSectionProps = {
  projectId: string;
  hashtag: string | null;
};

const viewFormatter = new Intl.NumberFormat("pt-BR");

function formatDelta(delta: number | null): string {
  if (delta === null) return "Primeira medição";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${viewFormatter.format(delta)} nas últimas 24h`;
}

function formatDate(value: string): string {
  return new Date(`${value}T12:00:00`).toLocaleDateString("pt-BR");
}

function RankingVideoCard({
  projectId,
  video,
  position,
  periodLabel,
}: {
  projectId: string;
  video: HashtagRankingVideo;
  position: number;
  periodLabel: string;
}) {
  return (
    <article className="grid gap-4 rounded-lg border border-slate-200 p-4 sm:grid-cols-[2rem_10rem_1fr] sm:items-center">
      <span className="text-lg font-bold text-indigo-700">#{position}</span>
      <div className="relative aspect-video overflow-hidden rounded-md bg-slate-100">
        {video.thumbnail_url ? (
          <Image
            src={video.thumbnail_url}
            alt={`Thumbnail de ${video.title}`}
            fill
            sizes="160px"
            className="object-cover"
          />
        ) : (
          <div className="grid h-full place-items-center text-xs text-slate-500">Sem thumbnail</div>
        )}
      </div>
      <div className="min-w-0">
        <h3 className="line-clamp-2 font-semibold text-slate-900">{video.title}</h3>
        <p className="mt-1 text-sm text-slate-600">{video.channel_title}</p>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <span className="font-medium text-slate-800">
            {viewFormatter.format(video.views)} views
          </span>
          <span
            className={
              video.view_delta !== null && video.view_delta > 0
                ? "font-medium text-emerald-700"
                : "text-slate-600"
            }
          >
            {periodLabel}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap gap-4 text-sm font-semibold">
          <a
            href={video.youtube_url}
            target="_blank"
            rel="noreferrer"
            className="text-indigo-700 hover:text-indigo-800"
          >
            Abrir no YouTube ↗
          </a>
          <a
            href={hashtagRankingDownloadUrl(projectId, video.id)}
            target="_blank"
            rel="noreferrer"
            className="text-indigo-700 hover:text-indigo-800"
          >
            Baixar vídeo
          </a>
        </div>
      </div>
    </article>
  );
}

export function HashtagRankingSection({ projectId, hashtag }: HashtagRankingSectionProps) {
  const [metric, setMetric] = useState<YoutubeRankingMetric>("view_delta");
  const [ranking, setRanking] = useState<HashtagRanking | null>(null);
  const [weeklyRanking, setWeeklyRanking] = useState<HashtagWeeklyRanking | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(hashtag));
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!hashtag) {
      setRanking(null);
      setIsLoading(false);
      return;
    }

    let active = true;
    setIsLoading(true);
    setError(null);
    Promise.all([getHashtagRanking(projectId, metric), getWeeklyHashtagRanking(projectId)])
      .then(([dailyResult, weeklyResult]) => {
        if (active) {
          setRanking(dailyResult);
          setWeeklyRanking(weeklyResult);
        }
      })
      .catch((requestError: unknown) => {
        if (active) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "Não foi possível carregar o ranking.",
          );
        }
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });

    return () => {
      active = false;
    };
  }, [hashtag, metric, projectId]);

  async function handleRefresh(): Promise<void> {
    setIsRefreshing(true);
    setError(null);
    try {
      await refreshHashtagRanking(projectId);
      const [dailyResult, weeklyResult] = await Promise.all([
        getHashtagRanking(projectId, metric),
        getWeeklyHashtagRanking(projectId),
      ]);
      setRanking(dailyResult);
      setWeeklyRanking(weeklyResult);
    } catch (requestError: unknown) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Não foi possível atualizar o ranking.",
      );
    } finally {
      setIsRefreshing(false);
    }
  }

  return (
    <section className="mt-8 max-w-3xl rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-wider text-indigo-600">
            Ranking diário
          </p>
          <h2 className="mt-1 text-xl font-bold text-slate-900">
            Top vídeos da hashtag hoje
          </h2>
          <p className="mt-2 text-sm text-slate-600">
            {hashtag
              ? `Vídeos encontrados para ${hashtag}.`
              : "Cadastre uma hashtag no projeto para ativar este ranking."}
          </p>
        </div>
        {hashtag && (
          <div className="flex flex-col items-stretch gap-2 sm:items-end">
            <button
              type="button"
              onClick={handleRefresh}
              disabled={isRefreshing}
              className="rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-indigo-700 disabled:cursor-wait disabled:opacity-60"
            >
              {isRefreshing ? "Buscando no YouTube..." : "Atualizar ranking agora"}
            </button>
            <div className="inline-flex self-start rounded-lg border border-slate-200 bg-slate-50 p-1 text-sm">
              <button
                type="button"
                aria-pressed={metric === "view_delta"}
                onClick={() => setMetric("view_delta")}
                className={`rounded-md px-3 py-2 font-semibold ${
                  metric === "view_delta"
                    ? "bg-white text-indigo-700 shadow-sm"
                    : "text-slate-600 hover:text-slate-900"
                }`}
              >
                Em alta 24h
              </button>
              <button
                type="button"
                aria-pressed={metric === "views"}
                onClick={() => setMetric("views")}
                className={`rounded-md px-3 py-2 font-semibold ${
                  metric === "views"
                    ? "bg-white text-indigo-700 shadow-sm"
                    : "text-slate-600 hover:text-slate-900"
                }`}
              >
                Mais vistos
              </button>
            </div>
          </div>
        )}
      </div>

      {isLoading && <p className="mt-6 text-sm text-slate-600">Carregando ranking...</p>}
      {error && <p className="mt-6 rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</p>}
      {!isLoading && !error && hashtag && ranking?.videos.length === 0 && (
        <p className="mt-6 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">
          O ranking ainda não foi processado. Ele aparecerá depois da primeira execução do job
          diário.
        </p>
      )}

      {!isLoading &&
        !error &&
        ranking &&
        ranking.snapshot_date &&
        ranking.videos.length > 0 && (
        <>
          <p className="mt-5 text-xs font-medium uppercase tracking-wide text-slate-500">
            Snapshot de {formatDate(ranking.snapshot_date)}
          </p>
          <div className="mt-3 space-y-3">
            {ranking.videos.map((video, index) => (
              <RankingVideoCard
                key={video.id}
                projectId={projectId}
                video={video}
                position={index + 1}
                periodLabel={formatDelta(video.view_delta)}
              />
            ))}
          </div>
          <p className="mt-4 text-xs leading-relaxed text-slate-500">
            O link de download é gerado na hora e pode expirar. Baixe somente vídeos que você tem
            autorização para usar.
          </p>
        </>
      )}

      {!isLoading && hashtag && (
        <div className="mt-8 border-t border-slate-200 pt-7">
          <p className="text-sm font-semibold uppercase tracking-wider text-amber-600">
            Destaques da semana
          </p>
          <h3 className="mt-1 text-lg font-bold text-slate-900">Top 3 dos últimos 7 dias</h3>
          <p className="mt-2 text-sm text-slate-600">
            Ordenado pelo crescimento de views entre os snapshots da semana.
          </p>

          {weeklyRanking?.videos.length ? (
            <>
              {weeklyRanking.period_start && weeklyRanking.period_end && (
                <p className="mt-5 text-xs font-medium uppercase tracking-wide text-slate-500">
                  {formatDate(weeklyRanking.period_start)} até {formatDate(weeklyRanking.period_end)}
                </p>
              )}
              <div className="mt-3 space-y-3">
                {weeklyRanking.videos.map((video, index) => (
                  <RankingVideoCard
                    key={video.id}
                    projectId={projectId}
                    video={{ ...video, view_delta: video.weekly_view_delta }}
                    position={index + 1}
                    periodLabel={
                      video.weekly_view_delta === null
                        ? "Aguardando novo snapshot"
                        : `${video.weekly_view_delta > 0 ? "+" : ""}${viewFormatter.format(video.weekly_view_delta)} na semana`
                    }
                  />
                ))}
              </div>
            </>
          ) : (
            <p className="mt-5 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">
              Ainda não há histórico semanal. Use “Atualizar ranking agora” e os dados serão
              acumulados diariamente.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
