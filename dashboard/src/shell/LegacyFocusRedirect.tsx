import { Navigate, useLocation } from "react-router-dom";

/** Preserve already-posted links while every viewport uses the same dashboard. */
export default function LegacyFocusRedirect() {
  const { pathname, search, hash } = useLocation();
  const tail = pathname.slice("/focus".length);
  const params = new URLSearchParams(search);
  let destination = "/command-center";
  if (/^\/(tasks|sessions|reports|asks|reviews|escalations|batches)\/[^/]+\/?$/.test(tail)) {
    destination = tail;
  } else if (tail === "/inbox" || tail === "/inbox/") {
    destination = "/inbox";
  } else if (tail === "/conversations" || tail === "/conversations/") {
    destination = "/conversations";
  } else if (/^\/conversations\/[^/]+\/?$/.test(tail)) {
    destination = "/conversations";
    params.set("conversation", decodeURIComponent(tail.split("/")[2]!));
  } else if ((tail === "" || tail === "/") && params.has("project")) {
    destination = `/projects/${encodeURIComponent(params.get("project")!)}/tasks-knowledge`;
    params.delete("project");
    params.set("kind", "task");
  }
  return <Navigate replace to={`${destination}${params.size ? `?${params}` : ""}${hash}`} />;
}
