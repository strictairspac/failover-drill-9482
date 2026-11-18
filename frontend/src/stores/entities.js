import {nodesAPI,scenariosAPI,drillsAPI} from '../api/entities.js';
import {sandboxAPI} from '../api/sandbox.js';
export const store={nodes:[],scenarios:[],drills:[],sandbox:null,async refresh(){[this.nodes,this.scenarios,this.drills,this.sandbox]=await Promise.all([nodesAPI.list(),scenariosAPI.list(),drillsAPI.list(),sandboxAPI.list()]);}};
