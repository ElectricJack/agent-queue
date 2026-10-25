declare const process: { versions: { node: string } };
if (process.versions.node !== "24.10.0") throw new Error(process.versions.node);
const value: { answer: number } = { answer: 42 };
if (value.answer !== 42) throw new Error("TypeScript smoke failed");
console.log("TypeScript 5.7.3 / Node 24.10.0: OK");
