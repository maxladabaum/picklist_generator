"""Manual replacement barcode controls and shared reservation integration."""
from copy import deepcopy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from barcode_registry import BarcodeRegistry, decode, from_rgb, layout, location, record
from image_encoding import default_barcode_schema, validate_barcode_schema, SELECTABLE_SCHEMES


class BarcodeGuard(ttk.LabelFrame):
    def __init__(self, parent, app, registry_path):
        super().__init__(parent, text='Barcode collision protection', padding=5)
        self.app = app
        self.registry = BarcodeRegistry(registry_path)
        self.enabled = tk.BooleanVar(value=app.config.get('manual_barcode_enabled', False))
        self.schema = validate_barcode_schema(app.config.get('manual_barcode_schema', default_barcode_schema()))
        self.experiment = app.rgb_image_view.barcode_experiment
        self.count = tk.StringVar(value=str(app.config.get('manual_barcode_bits',6)))
        self.source = tk.StringVar(value='LowConc PAINT R1')
        self._last_experiment = self.experiment.get()
        self.status = tk.StringVar()
        self.schema_name = tk.StringVar(value=self.schema['name'])
        ttk.Checkbutton(self, text='Use barcodes for replacements',
                        variable=self.enabled).pack(anchor='w')
        top = ttk.Frame(self); top.pack(fill='x')
        ttk.Label(top,text='Experiment / pool').pack(side='left')
        ttk.Entry(top,textvariable=self.experiment,width=18).pack(side='left',padx=4)
        ttk.Label(top,text='Bits').pack(side='left')
        ttk.Combobox(top,textvariable=self.count,values=('6','7','9'),state='readonly',width=3).pack(side='left',padx=4)
        ttk.Label(top,textvariable=self.schema_name,wraplength=180).pack(side='left',padx=4)
        for label, callback in [('Use RGB schema',self.use_rgb),('Load schema…',self.load_schema),
                                ('Import used codes…',self.import_log),('Registry…',self.show_registry)]:
            ttk.Button(top,text=label,command=callback).pack(side='left',padx=3)
        actions = ttk.Frame(self); actions.pack(fill='x',pady=3)
        ttk.Label(actions,text='Apply unused code to').pack(side='left')
        ttk.Combobox(actions,textvariable=self.source,values=[v.title for v in app.set_views],state='readonly',width=20).pack(side='left',padx=4)
        ttk.Button(actions,text='Select an unused code + fiducials',command=self.select_unused).pack(side='left',padx=4)
        ttk.Label(actions,textvariable=self.status,wraplength=500).pack(side='left',padx=4)
        ttk.Label(self,text='Optional for replacements. When enabled, checks PAINT selections across all replacement tabs. Use one experiment name for everything pooled/decoded together.').pack(anchor='w')
        self.sync_storage()
        legacy_count=sum(not item.get('barcode_record') and not item.get('image_design') for item in app._storage_items())
        if legacy_count:
            ttk.Label(self,text=f'{legacy_count} older manual runs lack barcode metadata; import their used-code log before treating codes as available.',
                      foreground='#a40000',wraplength=1000).pack(anchor='w')
        self.experiment.trace_add('write',self.changed)
        self.count.trace_add('write',self.changed)
        self.enabled.trace_add('write',self.changed)
        self.refresh()

    def changed(self,*_args):
        self.app.config.update(barcode_experiment=self.experiment.get(),manual_barcode_bits=int(self.count.get()),manual_barcode_schema=deepcopy(self.schema),
                               manual_barcode_enabled=self.enabled.get())
        self.app._schedule_config_save()
        self.app._invalidate_preview()
        if self.experiment.get() != self._last_experiment:
            self._last_experiment = self.experiment.get()
            self.app.rgb_image_view._invalidate()
        self.refresh()

    def sync_storage(self):
        entries=[]
        for item in self.app._storage_items():
            if item.get('barcode_record'):
                entries.append(item['barcode_record'])
            elif item.get('image_design'):
                entries.append(from_rgb(item,item.get('barcode_experiment','Default')))
        self.registry.reserve(entries,check=False)

    def fractions(self):
        fractions={}
        for view in self.app.set_views:
            for name in view.selected():
                site=location(name) or getattr(view, 'r1_locations', {}).get(name)
                if site:
                    value=view.site_fractions.get(name,1.0)
                    if site in fractions and fractions[site] != value:
                        raise ValueError('Conflicting PAINT selections at '+site)
                    fractions[site]=value
        return fractions

    def manual_record(self, run_id='preview', run_name='Manual run', plate='', wells=()):
        return record(self.schema,int(self.count.get()),self.fractions(),self.experiment.get(),run_id,run_name,plate,wells)

    def check_manual(self):
        if not self.enabled.get():
            return None
        entry=self.manual_record()
        self.registry.check(entry)
        return entry

    def refresh(self):
        if not self.enabled.get():
            self.status.set('No barcode — replacements are unrestricted')
            return
        try:
            entry=self.check_manual()
            self.status.set('Available · '+entry['bits'])
        except ValueError as exc:
            self.status.set(str(exc))

    def use_rgb(self):
        view=self.app.rgb_image_view
        self.apply_schema(view.barcode_schema, SELECTABLE_SCHEMES[view._scheme_id()].bit_count)

    def apply_schema(self,payload,count=None):
        if 'logical_model' in payload or 'physical_rows' in payload:
            from barcode_template_schema import from_template
            payload,count=from_template(payload,self.schema)
        if count is None:
            if 'seven_bit' in payload and 'six_bit' not in payload and 'nine_bit' not in payload:
                count=7
            elif 'nine_bit' in payload and 'six_bit' not in payload:
                count=9
            elif 'six_bit' in payload and 'nine_bit' not in payload:
                count=6
            else:
                count=int(self.count.get())
        value=validate_barcode_schema(payload,count)
        self.schema=value
        self.schema_name.set(value['name'])
        self.count.set(str(count))
        self.enabled.set(True)
        self.changed()

    def load_schema(self):
        path=filedialog.askopenfilename(parent=self,title='Load barcode + fiducial schema',filetypes=[('JSON','*.json')])
        if path:
            try:self.apply_schema(json.loads(Path(path).read_text()))
            except (ValueError,KeyError,OSError,TypeError) as exc:messagebox.showerror('Cannot load schema',str(exc),parent=self)

    def import_log(self):
        path=filedialog.askopenfilename(parent=self,title='Import RGB manifest or barcode registry',filetypes=[('JSON','*.json')])
        if path:
            try:
                count=self.registry.import_payload(json.loads(Path(path).read_text()),self.experiment.get().strip())
                self.refresh()
                messagebox.showinfo('Codes imported','{} reservations imported into {}.'.format(count,self.experiment.get()),parent=self)
            except (ValueError,KeyError,OSError,TypeError) as exc:messagebox.showerror('Cannot import codes',str(exc),parent=self)

    def select_unused(self):
        try:
            groups=layout(self.schema,int(self.count.get()))
            controlled={site for group in groups for site in group} | set(self.schema.get('always_on_sites',[]))
            view=next(v for v in self.app.set_views if v.title==self.source.get())
            names={location(name):name for name in view.variables if location(name)}
            if any(site not in names or not view.selectable.get(names[site]) for site in controlled):
                raise ValueError('The selected source sheet lacks one or more barcode/fiducial sites.')
            for code in range(2**len(groups)):
                bits=format(code,'0{}b'.format(len(groups)))
                fractions={site:float(bit) for bit,group in zip(bits,groups) for site in group}
                fractions.update({site:1.0 for site in self.schema.get('always_on_sites',[])})
                candidate=record(self.schema,len(groups),fractions,self.experiment.get(),'unused-candidate')
                try:self.registry.check(candidate)
                except ValueError as exc:
                    if 'Incompatible' in str(exc):raise
                    continue
                break
            else:raise ValueError('All codes in this schema are reserved for this experiment.')
            # Replace only schema-controlled sites; preserve image/other selections.
            for other in self.app.set_views:
                for name,var in other.variables.items():
                    if location(name) in controlled:
                        var.set(False);other.site_fractions.pop(name,None);other.site_concentrations_nm.pop(name,None)
                        other._paint_square(name)
                other._update_selection_summary()
            for site,value in fractions.items():
                name=names[site]
                view.variables[name].set(bool(value))
                if value:view.site_fractions[name]=1.0
                view._paint_square(name)
            view._update_selection_summary()
            self.enabled.set(True)
            self.app._selections_changed()
        except (ValueError,StopIteration) as exc:messagebox.showerror('Cannot choose code',str(exc),parent=self)

    def attach_rgb(self,runs,experiment=None):
        experiment=experiment or self.experiment.get()
        entries=[from_rgb(run,experiment) for run in runs]
        for index,entry in enumerate(entries):self.registry.check(entry,entries[:index])
        for run,entry in zip(runs,entries):
            run['barcode_record']=entry;run['barcode_experiment']=experiment
        return entries

    def reserve_runs(self,runs):
        entries=[run['barcode_record'] for run in runs]
        self.registry.reserve(entries)
        self.refresh()

    def check_preview(self):
        entry=getattr(self.app,'last_barcode_record',None)
        if entry:self.registry.check(entry)

    def reserve_manual(self,run_name=None):
        entry=getattr(self.app,'last_barcode_record',None)
        if entry:
            if run_name:entry['run_name']=run_name
            self.registry.reserve([entry]);self.refresh()

    def show_registry(self):
        window=tk.Toplevel(self);window.title('Barcode reservations — '+self.experiment.get())
        columns=('Run','Code','Plate','Wells','Origin','State')
        tree=ttk.Treeview(window,columns=columns,show='headings',selectmode='browse')
        for col in columns:tree.heading(col,text=col);tree.column(col,width=130)
        tree.pack(fill='both',expand=True)
        entries=[r for r in self.registry.entries if r['experiment']==self.experiment.get().strip()]
        def redraw():
            tree.delete(*tree.get_children())
            for i,r in enumerate(entries):tree.insert('', 'end',iid=str(i),values=(r['run_name'],r['bits'],r['destination_plate_name'],','.join(r['destination_wells']),r['origin'],'Released' if r.get('released') else 'Reserved'))
        def release():
            if not tree.selection():return
            entry=entries[int(tree.selection()[0])]
            if entry.get('released'):return
            if messagebox.askyesno('Release barcode reservation?', 'This permits code reuse in the same experiment. Release {} ({})?'.format(entry['bits'],entry['run_name']),parent=window):
                self.registry.release(entry);redraw();self.refresh()
        def export():
            path=filedialog.asksaveasfilename(parent=window,defaultextension='.json',initialfile='barcode_registry.json')
            if path:
                try:Path(path).write_text(json.dumps(self.registry.state,indent=2)+'\n')
                except OSError as exc:messagebox.showerror('Cannot export registry',str(exc),parent=window)
        actions=ttk.Frame(window);actions.pack(fill='x')
        ttk.Button(actions,text='Release selected reservation…',command=release).pack(side='left')
        ttk.Button(actions,text='Export registry…',command=export).pack(side='left')
        ttk.Label(window,text='Reservations survive removal from storage. Released records remain in this audit log.').pack()
        redraw()
