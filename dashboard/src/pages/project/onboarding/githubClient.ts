/** Typed boundary for GitHub discovery commands used by the onboarding wizard. */
import {
  getGithubAuthStatus,
  listGithubOwners,
  searchGithubRepositories,
  type GithubAuthStatusResponse,
  type GithubOwner,
  type GithubRepository,
} from "../../../api/client";

export async function githubAuthStatus(): Promise<GithubAuthStatusResponse> {
  return (await getGithubAuthStatus({ body: {}, throwOnError: true })).data;
}

export async function githubOwners(): Promise<GithubOwner[]> {
  return (await listGithubOwners({ body: {}, throwOnError: true })).data.owners ?? [];
}

export interface GithubSearchPage {
  repositories: GithubRepository[];
  nextCursor: string | null;
}

export async function searchGithub(query: string, cursor: string | null = null): Promise<GithubSearchPage> {
  const { data } = await searchGithubRepositories({
    body: { query, cursor, limit: 20 },
    throwOnError: true,
  });
  return { repositories: data.repositories ?? [], nextCursor: data.next_cursor ?? null };
}
