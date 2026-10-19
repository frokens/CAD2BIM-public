export default async function Library({ searchParams }: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const supplied = (await searchParams) || {};
  const context = new URLSearchParams();
  for (const key of ["source", "task_id", "run_id"]) {
    const value = supplied[key];
    if (typeof value === "string") context.set(key, value);
  }
  const source = `/cad2bim/library.html${context.size ? "?" + context.toString() : ""}`;
  return (
    <main className="prototype-frame">
      <iframe src={source} title="CAD2BIM 几何与产品族" />
    </main>
  );
}
