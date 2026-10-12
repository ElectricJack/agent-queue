/** A worker rung id is `<class>-<harness>`: show it as the two words it is. */
export function profileTags(profileId?: string | null, intelligenceClass?: string | null): string[] {
  if (profileId && intelligenceClass && profileId.startsWith(`${intelligenceClass}-`)) {
    return [intelligenceClass, profileId.slice(intelligenceClass.length + 1)];
  }
  return [...new Set([profileId, intelligenceClass].filter((tag): tag is string => Boolean(tag)))];
}
