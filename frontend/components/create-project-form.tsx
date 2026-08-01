"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { AppShell } from "@/components/app-shell";
import { createProject } from "@/lib/api";

export function CreateProjectForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [hashtag, setHashtag] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);

    try {
      const project = await createProject({ name, description, hashtag });
      router.push(`/projects/${project.id}`);
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Não foi possível criar o projeto.");
      setIsSubmitting(false);
    }
  }

  return (
    <AppShell>
      <div className="mx-auto max-w-2xl">
        <Link href="/" className="text-sm font-medium text-indigo-700 hover:text-indigo-800">
          ← Voltar para projetos
        </Link>
        <section className="mt-5 rounded-xl border border-slate-200 bg-white p-6 sm:p-8">
          <p className="text-sm font-semibold uppercase tracking-wider text-indigo-600">Novo Projeto</p>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-slate-900">Crie um espaço para seu vídeo</h1>
          <p className="mt-2 text-slate-600">Os dados podem ser usados para organizar os próximos passos.</p>

          <form className="mt-8 space-y-5" onSubmit={handleSubmit}>
            <label className="block">
              <span className="text-sm font-medium text-slate-800">Nome do projeto</span>
              <input
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-slate-900"
                placeholder="Ex.: Podcast com Ana"
              />
            </label>

            <label className="block">
              <span className="text-sm font-medium text-slate-800">Descrição</span>
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={4}
                className="mt-2 w-full resize-y rounded-lg border border-slate-300 px-3 py-2.5 text-slate-900"
                placeholder="Uma breve descrição do conteúdo"
              />
            </label>

            <label className="block">
              <span className="text-sm font-medium text-slate-800">Hashtag</span>
              <input
                value={hashtag}
                onChange={(event) => setHashtag(event.target.value)}
                className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-slate-900"
                placeholder="#podcast"
              />
            </label>

            {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full rounded-lg bg-indigo-600 px-4 py-3 font-semibold text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300"
            >
              {isSubmitting ? "Criando..." : "Criar"}
            </button>
          </form>
        </section>
      </div>
    </AppShell>
  );
}
