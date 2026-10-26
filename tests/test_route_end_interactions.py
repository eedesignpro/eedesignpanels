"""Exercise the route-end controls using the JavaScript served to the user."""
import shutil
import unittest

from eedesignpanels.preview.page_served import PREP_JS
from test_preview_journal import run_js


@unittest.skipUnless(shutil.which("node"), "Node is required for interaction checks")
class RouteEndInteractionsTest(unittest.TestCase):
    def test_select_enable_enter_and_disable(self):
        source = PREP_JS[PREP_JS.index('function drawRouteEnds('):
                         PREP_JS.index('// The declared numbers, as the config')]
        output = run_js(r'''
const markers = [], nodes = {}, posts = [];
function node() {
  return {value:'', checked:false, disabled:false, style:{}, dataset:{}, attrs:{}, events:{},
    replaceChildren(){this.value='';},
    appendChild(item){if (!this.value) this.value=item.value;},
    setAttribute(k,v){this.attrs[k]=v;},
    remove(){this.removed=true;},
    addEventListener(k,f){this.events[k]=f;},
    checkValidity(){return true;},
    click(){if (!nodes['pb-route-fields'].disabled && this.onclick) this.onclick();}
  };
}
const svg = {appendChild(item){markers.push(item);}};
const viewport = {querySelector(){return svg;}};
const document = {
  getElementById(id){return nodes[id] ||= node();},
  querySelectorAll(selector){return selector === '.viewport' ? [viewport] : markers.filter(x=>!x.removed);},
  createElement(){return node();}, createElementNS(){return node();}
};
const PB = {params:{custom_route_ends:false,route_end_overrides:{b:2}}};
function post(path,payload){posts.push(payload);}
function paintFold(group,collapsed){group.collapsed=collapsed;}
''' + source + r'''
const ends=[{end_id:'a',at:[1,2],layer:'User.36'}, {end_id:'b',at:[3,4],layer:'User.36'}];
drawRouteEnds(ends);
const off = {disabled:nodes['pb-route-fields'].disabled,
  colors:markers.map(x=>x.attrs.stroke), pointers:markers.map(x=>x.style.pointerEvents)};
nodes['pb-custom-route-ends'].checked=true;
nodes['pb-custom-route-ends'].onchange();
PB.params.custom_route_ends=true;
drawRouteEnds(ends);
const live=markers.filter(x=>!x.removed);
live[1].events.click({stopPropagation(){}});
const selected={end:nodes['pb-route-end'].value,colors:live.map(x=>x.attrs.stroke),
  distance:nodes['pb-route-distance'].value, collapsed:nodes['pb-sec-router'].collapsed};
nodes['pb-route-distance'].value='4.25';
nodes['pb-route-distance'].events.input();
nodes['pb-route-distance'].events.keydown({key:'Enter',preventDefault(){},stopPropagation(){}});
const mode=nodes['pb-route-mode'].value;
PB.params.custom_route_ends=false;
drawRouteEnds(ends);
console.log(JSON.stringify({off,selected,mode,posts,disabled:nodes['pb-route-fields'].disabled,
 retained:PB.params.route_end_overrides}));
''')
        self.assertTrue(output['off']['disabled'])
        self.assertEqual(output['off']['colors'], ['#909090', '#909090'])
        self.assertEqual(output['off']['pointers'], ['none', 'none'])
        self.assertEqual(output['selected'], {'end': 'b', 'colors': ['#909090', '#ff55ff'],
                                              'distance': 2, 'collapsed': False})
        self.assertEqual(output['mode'], 'custom')
        self.assertEqual(output['posts'], [{'custom_route_ends': True}, {'route_end_overrides': {'b': 4.25}}])
        self.assertTrue(output['disabled'])
        self.assertEqual(output['retained'], {'b': 2})
