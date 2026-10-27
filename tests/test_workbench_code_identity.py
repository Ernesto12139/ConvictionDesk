"""An installed package must never borrow a surrounding unrelated Git identity."""
from pathlib import Path
import subprocess

from market_agent import workbench_trace


def test_installed_package_inside_another_git_repository_is_unknown(tmp_path, monkeypatch):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Synthetic Fixture',
        '-c','user.email=fixture@example.com','commit','--allow-empty','-qm','Synthetic identity trap'],check=True)
    package=tmp_path/'venv/Lib/site-packages/market_agent'
    package.mkdir(parents=True)
    module=package/'workbench_trace.py'
    module.write_text('# synthetic installed runtime\n',encoding='utf-8')
    monkeypatch.setattr(workbench_trace,'__file__',str(module))
    identity=workbench_trace.workbench_code_identity()
    assert identity.commit=='UNKNOWN'
    assert identity.source=='UNKNOWN'
    assert identity.build.startswith('runtime-sha256:')


def test_real_source_identity_belongs_to_its_own_repository():
    identity=workbench_trace.workbench_code_identity()
    root=Path(workbench_trace.__file__).resolve().parents[2]
    expected=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    assert identity.commit==expected
    assert identity.source=='GIT'
