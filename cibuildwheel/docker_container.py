import io
import json
import shlex
import subprocess
import sys
import uuid
from os import PathLike
from pathlib import Path, PurePath
from typing import IO, Dict, List, Optional, Sequence, TextIO, Union


class DockerContainer:
    '''
    An object that represents a running Docker container.

    Intended for use as a context manager e.g.
    `with DockerContainer('ubuntu') as docker:`

    A bash shell is running in the remote container. When `call()` is invoked,
    the command is relayed to the remote shell, and the results are streamed
    back to cibuildwheel.
    '''
    UTILITY_PYTHON = '/opt/python/cp38-cp38/bin/python'

    process: subprocess.Popen
    bash_stdin: IO[str]
    bash_stdout: IO[str]

    def __init__(self, docker_image: str):
        self.docker_image = docker_image

    def __enter__(self) -> 'DockerContainer':
        self.container_name = f'cibuildwheel-{uuid.uuid4()}'
        subprocess.run(
            [
                'docker', 'create',
                '--env', 'CIBUILDWHEEL',
                '--name', self.container_name,
                '-i',
                '-v', '/:/host',  # ignored on CircleCI
                self.docker_image
            ],
            check=True,
        )
        process = subprocess.Popen(
            [
                'docker', 'start',
                '--attach', '--interactive',
                self.container_name,
            ],
            encoding='utf8',
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # make the input buffer large enough to carry a lot of environment
            # variables. We choose 256kB.
            bufsize=262144,
        )
        self.process = process
        assert process.stdin and process.stdout
        self.bash_stdin = process.stdin
        self.bash_stdout = process.stdout
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.bash_stdin.close()
        self.process.terminate()
        self.process.wait()

        subprocess.run(['docker', 'rm', '--force', '-v', self.container_name])
        self.container_name = None

    def copy_into(self, from_path: Path, to_path: PurePath) -> None:
        # `docker cp` causes 'no space left on device' error when
        # a container is running and the host filesystem is
        # mounted. https://github.com/moby/moby/issues/38995
        # Use `docker exec` instead.
        if from_path.is_dir():
            self.call(['mkdir', '-p', to_path])
            subprocess.run(
                f'tar cf - . | docker exec -i {self.container_name} tar -xC {to_path} -f -',
                shell=True,
                check=True,
                cwd=from_path)
        else:
            subprocess.run(
                f'cat {from_path} | docker exec -i {self.container_name} sh -c "cat > {to_path}"',
                shell=True,
                check=True)

    def copy_out(self, from_path: PurePath, to_path: Path) -> None:
        # note: we assume from_path is a dir
        to_path.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            f'docker exec -i {self.container_name} tar -cC {from_path} -f - . | tar -xf -',
            shell=True,
            check=True,
            cwd=to_path
        )

    def glob(self, pattern: PurePath) -> List[PurePath]:
        path_strs = json.loads(self.call([
            self.UTILITY_PYTHON,
            '-c',
            f'import sys, json, glob; json.dump(glob.glob({str(pattern)!r}), sys.stdout)'
        ], capture_output=True))

        return [PurePath(p) for p in path_strs]

    def call(self, args: Sequence[Union[str, PathLike]], env: Dict[str, str] = {},
             capture_output=False, cwd: Optional[Union[str, PathLike]] = None) -> str:
        env_exports = '\n'.join(f'export {k}={v}' for k, v in env.items())
        chdir = f'cd {cwd}' if cwd else ''
        command = ' '.join(shlex.quote(str(a)) for a in args)
        end_of_message = str(uuid.uuid4())

        # log the command we're executing
        print(f'    + {command}')

        # Write a command to the remote shell. First we write the
        # environment variables, exported inside the subshell. We change the
        # cwd, if that's required. Then, the command is written. Finally, the
        # remote shell is told to write a footer - this will show up in the
        # output so we know when to stop reading, and will include the
        # returncode of `command`.
        self.bash_stdin.write(f'''(
            {env_exports}
            {chdir}
            {command}
            printf "%04d%s\n" $? {end_of_message}
        )
        ''')
        self.bash_stdin.flush()

        if capture_output:
            output_io: TextIO = io.StringIO()
        else:
            output_io = sys.stdout

        while True:
            line = self.bash_stdout.readline()

            if line.endswith(end_of_message+'\n'):
                footer_offset = (
                    len(line)
                    - 1  # newline character
                    - len(end_of_message)  # delimiter
                    - 4  # 4 returncode decimals
                )
                returncode_str = line[footer_offset:footer_offset+4]
                returncode = int(returncode_str)
                # add the last line to output, without the footer
                output_io.write(line[0:footer_offset])
                break
            else:
                output_io.write(line)

        output = output_io.getvalue() if isinstance(output_io, io.StringIO) else None

        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, args, output)

        return output if output else ''

    def get_environment(self) -> Dict[str, str]:
        return json.loads(self.call([
            self.UTILITY_PYTHON,
            '-c',
            'import sys, json, os; json.dump(os.environ.copy(), sys.stdout)'
        ], capture_output=True))

    def environment_executor(self, command: str, environment: Dict[str, str]) -> str:
        # used as an EnvironmentExecutor to evaluate commands and capture output
        return self.call(shlex.split(command), env=environment)
