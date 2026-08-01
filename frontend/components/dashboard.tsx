"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { AppShell } from "@/components/app-shell";
import { deleteProject, getProjects } from "@/lib/api";
import type { Project } from "@/lib/types";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("pt-BR", { dateStyle: "medium" }).format(new Date(value));
}

function SearchIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5 fill-none stroke-current" strokeWidth="2">
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4 fill-none stroke-current" strokeWidth="2">
      <path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" />
    </svg>
  );
}

export function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [search, setSearch] = useState("");
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isDeleting, setIsDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getProjects()
      .then(setProjects)
      .catch((requestError: unknown) => {
        setError(requestError instanceof Error ? requestError.message : "Não foi possível carregar os projetos.");
      })
      .finally(() => setIsLoading(false));
  }, []);

  const filteredProjects = useMemo(() => {
    const normalizedSearch = search.trim().toLocaleLowerCase("pt-BR");
    if (!normalizedSearch) return projects;
    return projects.filter((project) =>
      [project.name, project.description, project.hashtag]
        .filter(Boolean)
        .some((value) => value?.toLocaleLowerCase("pt-BR").includes(normalizedSearch)),
    );
  }, [projects, search]);

  const hashtagCount = new Set(projects.map((project) => project.hashtag).filter(Boolean)).size;

  async function handleDeleteProject() {
    if (!projectToDelete) return;
    setIsDeleting(true);
    setError(null);
    try {
      await deleteProject(projectToDelete.id);
      setProjects((currentProjects) =>
        currentProjects.filter((project) => project.id !== projectToDelete.id),
      );
      setProjectToDelete(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível excluir o projeto.");
    } finally {
      setIsDeleting(false);
    }
  }

  return (
    <AppShell>
      <section className="overflow-hidden rounded-3xl bg-slate-950 px-6 py-8 text-white shadow-xl shadow-slate-200 sm:px-9 sm:py-10">
        <div className="relative z-10 flex flex-col gap-7 sm:flex-row sm:items-end sm:justify-between">
          <div className="max-w-2xl">
            <p className="mb-3 text-sm font-semibold uppercase tracking-[0.2em] text-indigo-300">Dashboard</p>
            <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">Transforme referências em novos cortes</h1>
            <p className="mt-3 max-w-xl text-sm leading-6 text-slate-300 sm:text-base">
              Organize seus vídeos, compare o áudio e acompanhe todos os cortes gerados em um só lugar.
            </p>
          </div>
          <Link
            href="/projects/new"
            className="inline-flex shrink-0 items-center justify-center rounded-xl bg-indigo-500 px-5 py-3 font-semibold text-white shadow-lg shadow-indigo-950/30 transition hover:bg-indigo-400"
          >
            <span className="mr-2 text-xl leading-none">+</span>
            Novo Projeto
          </Link>
        </div>
      </section>

      <section className="mt-6 grid gap-4 sm:grid-cols-3">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-medium text-slate-500">Total de projetos</p>
          <p className="mt-2 text-3xl font-bold text-slate-950">{projects.length}</p>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-medium text-slate-500">Hashtags organizadas</p>
          <p className="mt-2 text-3xl font-bold text-slate-950">{hashtagCount}</p>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm font-medium text-slate-500">Projeto mais recente</p>
          <p className="mt-2 truncate text-lg font-bold text-slate-950">
            {projects[0]?.name ?? "Nenhum ainda"}
          </p>
        </div>
      </section>

      <section className="mt-10">
        <div className="mb-5 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-2xl font-bold tracking-tight text-slate-950">Seus projetos</h2>
            <p className="mt-1 text-sm text-slate-500">Abra um projeto ou remova o que não precisa mais.</p>
          </div>
          <label className="flex h-11 w-full items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 text-slate-500 shadow-sm focus-within:border-indigo-400 sm:w-80">
            <SearchIcon />
            <span className="sr-only">Buscar projetos</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Buscar projeto ou hashtag"
              className="min-w-0 flex-1 border-0 bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
            />
          </label>
        </div>

        {isLoading && (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((item) => (
              <div key={item} className="h-56 animate-pulse rounded-2xl bg-slate-200" />
            ))}
          </div>
        )}

        {error && <p className="mb-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</p>}

        {!isLoading && !error && projects.length === 0 && (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-16 text-center">
            <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-indigo-50 text-2xl text-indigo-600">+</div>
            <h3 className="mt-4 text-lg font-semibold text-slate-950">Nenhum projeto criado</h3>
            <p className="mt-2 text-slate-500">Crie seu primeiro projeto para começar.</p>
          </div>
        )}

        {!isLoading && !error && projects.length > 0 && filteredProjects.length === 0 && (
          <div className="rounded-2xl border border-slate-200 bg-white px-6 py-12 text-center text-slate-500">
            Nenhum projeto corresponde à sua busca.
          </div>
        )}

        {!isLoading && !error && filteredProjects.length > 0 && (
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {filteredProjects.map((project) => (
              <article
                key={project.id}
                className="group flex min-h-60 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition hover:-translate-y-0.5 hover:border-indigo-200 hover:shadow-lg hover:shadow-indigo-100/60"
              >
                <Link href={`/projects/${project.id}`} className="flex flex-1 flex-col p-6">
                  <div className="flex items-start justify-between gap-3">
                    <span className="rounded-lg bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700">
                      {project.hashtag || "Sem hashtag"}
                    </span>
                    <span className="text-xs text-slate-400">{formatDate(project.created_at)}</span>
                  </div>
                  <h3 className="mt-5 text-xl font-bold text-slate-950 transition group-hover:text-indigo-700">
                    {project.name}
                  </h3>
                  <p className="mt-2 line-clamp-3 text-sm leading-6 text-slate-500">
                    {project.description || "Projeto pronto para receber o vídeo original e suas referências."}
                  </p>
                </Link>
                <div className="flex items-center justify-between border-t border-slate-100 px-6 py-3">
                  <Link href={`/projects/${project.id}`} className="text-sm font-semibold text-indigo-700 hover:text-indigo-900">
                    Abrir projeto →
                  </Link>
                  <button
                    type="button"
                    onClick={() => setProjectToDelete(project)}
                    className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-sm font-medium text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    aria-label={`Excluir projeto ${project.name}`}
                  >
                    <TrashIcon />
                    Excluir
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {projectToDelete && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/60 px-5 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="delete-project-title">
          <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl">
            <div className="grid h-12 w-12 place-items-center rounded-xl bg-red-50 text-red-600">
              <TrashIcon />
            </div>
            <h2 id="delete-project-title" className="mt-5 text-xl font-bold text-slate-950">Excluir “{projectToDelete.name}”?</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">
              O projeto, os vídeos enviados e os cortes gerados serão removidos permanentemente.
            </p>
            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                disabled={isDeleting}
                onClick={() => setProjectToDelete(null)}
                className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                Cancelar
              </button>
              <button
                type="button"
                disabled={isDeleting}
                onClick={handleDeleteProject}
                className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:cursor-wait disabled:opacity-60"
              >
                {isDeleting ? "Excluindo..." : "Excluir projeto"}
              </button>
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
