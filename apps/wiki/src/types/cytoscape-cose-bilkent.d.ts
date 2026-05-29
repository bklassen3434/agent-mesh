// cytoscape-cose-bilkent ships no types. It's a cytoscape layout extension
// registered via cytoscape.use(); we only need the default export to exist.
declare module 'cytoscape-cose-bilkent' {
  import type { Ext } from 'cytoscape';
  const ext: Ext;
  export default ext;
}
