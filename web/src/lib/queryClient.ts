import { QueryClient } from "@tanstack/react-query"

/**
 * The single QueryClient for the app, kept in its own module so that
 * `lib/auth.tsx` can reset it on logout / identity change without importing
 * `main.tsx` back (which would create a cycle).
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

/**
 * Cancel every in-flight request and drop all cached data.
 *
 * Called whenever the signed-in identity can change (logout, sign-in, user or
 * org switch) so that one account's cached data can never be rendered for, or
 * written into, another account's session. Cancelling first stops responses
 * that are already on the wire from repopulating the cache after the reset;
 * `clear()` then empties the query and mutation caches.
 */
export async function resetQueryCache(client: QueryClient = queryClient): Promise<void> {
  try {
    await client.cancelQueries()
  } catch {
    // Cancelling must never stop the cache from being cleared.
  }
  client.clear()
}
