import { describe, expect, it } from "vitest";
import {
  type GitIdentityFormFields,
  gitIdentityErrors,
  gitIdentityPayload,
  gitIdentitySaveError,
  inheritedGitIdentity,
  validateGitEmail,
  validateGitName,
} from "../gitIdentity";

const inheriting: GitIdentityFormFields = {
  git_identity_override: false,
  git_identity_name: "Ops Bot",
  git_identity_email: "ops@example.com",
};
const overridden: GitIdentityFormFields = {
  git_identity_override: true,
  git_identity_name: "Jane Doe",
  git_identity_email: "jane@example.com",
};

describe("git identity validation (mirrors src/git/identity.py)", () => {
  it("accepts ordinary names and addresses, including the dotless fallback", () => {
    expect(validateGitName("Jane Doe")).toBeNull();
    expect(validateGitName("  Jane Doe  ")).toBeNull();
    expect(validateGitEmail("jane@example.com")).toBeNull();
    expect(validateGitEmail("agent-queue@localhost")).toBeNull();
  });

  it("rejects empty values", () => {
    expect(validateGitName("   ")).toMatch(/must not be empty/);
    expect(validateGitEmail("")).toMatch(/must not be empty/);
  });

  it("rejects angle brackets", () => {
    expect(validateGitName("a<b")).toMatch(/must not contain '<' or '>'/);
    expect(validateGitEmail("a>b@example.com")).toMatch(/must not contain '<' or '>'/);
  });

  it("rejects an embedded newline or control character", () => {
    expect(validateGitName("Jane\nDoe")).toMatch(/single line/);
    expect(validateGitName("Jane\u0007Doe")).toMatch(/single line/);
    expect(validateGitName("Jane\u200bDoe")).toMatch(/single line/);
  });

  it("rejects punctuation Git strips from either end", () => {
    expect(validateGitName(".Jane")).toMatch(/punctuation Git strips/);
    expect(validateGitName("Jane;")).toMatch(/punctuation Git strips/);
    expect(validateGitName('"Jane"')).toMatch(/punctuation Git strips/);
    expect(validateGitEmail("jane@example.com.")).toMatch(/punctuation Git strips/);
  });

  it("rejects an email that is not one name@domain address", () => {
    expect(validateGitEmail("not-an-email")).toMatch(/name@domain/);
    expect(validateGitEmail("jane doe@example.com")).toMatch(/name@domain/);
    expect(validateGitEmail("a@b@c")).toMatch(/name@domain/);
  });

  it("enforces the length limits", () => {
    expect(validateGitName("a".repeat(200))).toBeNull();
    expect(validateGitName("a".repeat(201))).toMatch(/at most 200/);
    expect(validateGitEmail(`${"a".repeat(250)}@b.c`)).toBeNull();
    expect(validateGitEmail(`${"a".repeat(251)}@b.c`)).toMatch(/at most 254/);
  });

  it("gitIdentityErrors only validates while overriding", () => {
    expect(gitIdentityErrors({ ...inheriting, git_identity_name: "a<b" })).toEqual({});
    expect(
      gitIdentityErrors({ ...overridden, git_identity_name: "a<b", git_identity_email: "bad" }),
    ).toEqual({
      name: expect.stringMatching(/'<' or '>'/),
      email: expect.stringMatching(/name@domain/),
    });
  });
});

describe("gitIdentityPayload", () => {
  it("sends nothing for an unchanged identity", () => {
    expect(gitIdentityPayload(inheriting, inheriting)).toEqual({});
    expect(gitIdentityPayload(overridden, { ...overridden })).toEqual({});
    expect(
      gitIdentityPayload(overridden, { ...overridden, git_identity_name: " Jane Doe " }),
    ).toEqual({});
  });

  it("sends both fields, trimmed, for a new or changed override", () => {
    expect(
      gitIdentityPayload(inheriting, {
        git_identity_override: true,
        git_identity_name: " Jane Doe ",
        git_identity_email: "jane@example.com ",
      }),
    ).toEqual({ git_identity_name: "Jane Doe", git_identity_email: "jane@example.com" });
    expect(
      gitIdentityPayload(overridden, { ...overridden, git_identity_email: "j@example.org" }),
    ).toEqual({ git_identity_name: "Jane Doe", git_identity_email: "j@example.org" });
  });

  it("resets with both fields empty, never null", () => {
    expect(gitIdentityPayload(overridden, inheriting)).toEqual({
      git_identity_name: "",
      git_identity_email: "",
    });
  });
});

describe("gitIdentitySaveError", () => {
  it("attributes the typed route's 'name: …' / 'email: …' prose to the field", () => {
    expect(gitIdentitySaveError(new Error("API 422: name: must not contain '<' or '>'"))).toEqual({
      name: "Name must not contain '<' or '>'",
    });
    expect(
      gitIdentitySaveError(new Error("API 422: email: must be one address of the form name@domain")),
    ).toEqual({ email: "Email must be one address of the form name@domain" });
  });

  it("prefers a field in the error payload when the daemon sends one", () => {
    const err = Object.assign(new Error("API 422: email: bad"), {
      payload: { error_code: "invalid_git_identity", field: "git_identity_email", error: "email: bad" },
    });
    expect(gitIdentitySaveError(err)).toEqual({ email: "Email bad" });
  });

  it("reports pair and operator-only refusals as a section error", () => {
    expect(
      gitIdentitySaveError(
        new Error(
          "API 422: A project Git identity override needs both git_identity_name and git_identity_email; clear both to inherit the installation default.",
        ),
      ),
    ).toEqual({ general: expect.stringMatching(/^A project Git identity override/) });
    expect(
      gitIdentitySaveError(
        new Error("API 422: Git identity is operator configuration; agent sessions cannot change it"),
      ),
    ).toEqual({ general: expect.stringMatching(/operator configuration/) });
  });

  it("leaves unrelated failures to the generic error", () => {
    expect(gitIdentitySaveError(new Error("API 500: boom"))).toBeNull();
  });
});

describe("inheritedGitIdentity", () => {
  const fallback = { name: "Agent Queue", email: "agent-queue@localhost" };
  it("is the installation default when set, else the fallback", () => {
    expect(
      inheritedGitIdentity({
        name: "Jane Doe",
        email: "jane@example.com",
        source: "project",
        configured: true,
        installation: { name: "Ops Bot", email: "ops@example.com" },
        fallback,
      }),
    ).toEqual({ name: "Ops Bot", email: "ops@example.com", source: "installation" });
    expect(
      inheritedGitIdentity({ ...fallback, source: "fallback", configured: false, fallback }),
    ).toEqual({ ...fallback, source: "fallback" });
    expect(inheritedGitIdentity(null)).toBeNull();
  });
});
