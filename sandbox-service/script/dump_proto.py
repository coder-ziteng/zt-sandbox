import sys
sys.path.insert(0, r"E:\work\zt-Sandbox\sandbox-service\.venv\Lib\site-packages")
from e2b.envd.process import process_pb2
from e2b.envd.filesystem import filesystem_pb2
from google.protobuf import descriptor_pb2

def dump(d, name):
    out = [f"== {name}: {d.full_name}"]
    for f in d.fields:
        out.append(f"  {f.number}: {f.name} type={f.type} msg={f.message_type.full_name if f.message_type else ''} enum={f.enum_type.full_name if f.enum_type else ''}")
    return "\n".join(out)

lines = []
for m in process_pb2.DESCRIPTOR.message_types_by_name.values():
    lines.append(dump(m, m.name))
    for n in m.nested_types_by_name.values():
        lines.append(dump(n, n.full_name))
for e in process_pb2.DESCRIPTOR.enum_types_by_name.values():
    lines.append("== enum " + e.full_name + ": " + ", ".join(f"{v.name}={v.number}" for v in e.values))
lines.append("")
for m in filesystem_pb2.DESCRIPTOR.message_types_by_name.values():
    lines.append(dump(m, m.name))
for e in filesystem_pb2.DESCRIPTOR.enum_types_by_name.values():
    lines.append("== enum " + e.full_name + ": " + ", ".join(f"{v.name}={v.number}" for v in e.values))
print("\n".join(lines))
