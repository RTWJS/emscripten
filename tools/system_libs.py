from __future__ import print_function
import os, json, logging, zipfile, tarfile, glob, shutil
from . import shared
from subprocess import Popen, CalledProcessError
import subprocess, multiprocessing, re
from tools.shared import check_call

stdout = None
stderr = None

def call_process(cmd):
  proc = Popen(cmd, stdout=stdout, stderr=stderr)
  proc.communicate()
  if proc.returncode != 0:
    # Deliberately do not use CalledProcessError, see issue #2944
    raise Exception('Command \'%s\' returned non-zero exit status %s' % (' '.join(cmd), proc.returncode))

CORES = int(os.environ.get('EMCC_CORES') or multiprocessing.cpu_count())

def run_commands(commands):
  cores = min(len(commands), CORES)
  if cores <= 1:
    for command in commands:
      call_process(command)
  else:
    pool = shared.Building.get_multiprocessing_pool()
    # https://stackoverflow.com/questions/1408356/keyboard-interrupts-with-pythons-multiprocessing-pool, https://bugs.python.org/issue8296
    # 999999 seconds (about 11 days) is reasonably huge to not trigger actual timeout
    # and is smaller than the maximum timeout value 4294967.0 for Python 3 on Windows (threading.TIMEOUT_MAX)
    pool.map_async(call_process, commands, chunksize=1).get(999999)

def files_in_path(path_components, filenames):
  srcdir = shared.path_from_root(*path_components)
  return [os.path.join(srcdir, f) for f in filenames]

def calculate(temp_files, in_temp, stdout_, stderr_, forced=[]):
  global stdout, stderr
  stdout = stdout_
  stderr = stderr_

  # Check if we need to include some libraries that we compile. (We implement libc ourselves in js, but
  # compile a malloc implementation and stdlibc++.)

  def read_symbols(path):
    with open(path) as f:
      return shared.Building.parse_symbols(f.read()).defs

  default_opts = ['-Werror']

  # XXX We also need to add libc symbols that use malloc, for example strdup. It's very rare to use just them and not
  #     a normal malloc symbol (like free, after calling strdup), so we haven't hit this yet, but it is possible.
  libc_symbols = read_symbols(shared.path_from_root('system', 'lib', 'libc.symbols'))
  libcxx_symbols = read_symbols(shared.path_from_root('system', 'lib', 'libcxx', 'symbols'))
  libcxxabi_symbols = read_symbols(shared.path_from_root('system', 'lib', 'libcxxabi', 'symbols'))
  gl_symbols = read_symbols(shared.path_from_root('system', 'lib', 'gl.symbols'))
  al_symbols = read_symbols(shared.path_from_root('system', 'lib', 'al.symbols'))
  compiler_rt_symbols = read_symbols(shared.path_from_root('system', 'lib', 'compiler-rt.symbols'))
  pthreads_symbols = read_symbols(shared.path_from_root('system', 'lib', 'pthreads.symbols'))
  asmjs_pthreads_symbols = read_symbols(shared.path_from_root('system', 'lib', 'asmjs_pthreads.symbols'))
  wasm_libc_symbols = read_symbols(shared.path_from_root('system', 'lib', 'wasm-libc.symbols'))
  html5_symbols = read_symbols(shared.path_from_root('system', 'lib', 'html5.symbols'))

  # XXX we should disable EMCC_DEBUG when building libs, just like in the relooper

  def musl_internal_includes():
    return [
      '-I', shared.path_from_root('system', 'lib', 'libc', 'musl', 'src', 'internal'),
      '-I', shared.path_from_root('system', 'lib', 'libc', 'musl', 'arch', 'js'),
    ]

  def build_libc(lib_filename, files, lib_opts):
    o_s = []
    commands = []
    # Hide several musl warnings that produce a lot of spam to unit test build server logs.
    # TODO: When updating musl the next time, feel free to recheck which of their warnings might have been fixed, and which ones of these could be cleaned up.
    c_opts = ['-Wno-return-type', '-Wno-parentheses', '-Wno-ignored-attributes',
              '-Wno-shift-count-overflow', '-Wno-shift-negative-value',
              '-Wno-dangling-else', '-Wno-unknown-pragmas',
              '-Wno-shift-op-parentheses', '-Wno-string-plus-int',
              '-Wno-logical-op-parentheses', '-Wno-bitwise-op-parentheses',
              '-Wno-visibility', '-Wno-pointer-sign', '-Wno-absolute-value',
              '-Wno-empty-body']
    for src in files:
      o = in_temp(os.path.basename(src) + '.o')
      commands.append([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', src), '-o', o] + musl_internal_includes() + default_opts + c_opts + lib_opts)
      o_s.append(o)
    run_commands(commands)
    shared.Building.link(o_s, in_temp(lib_filename))
    return in_temp(lib_filename)

  def build_libcxx(src_dirname, lib_filename, files, lib_opts, has_noexcept_version=False):
    o_s = []
    commands = []
    opts = default_opts + lib_opts
    if has_noexcept_version and shared.Settings.DISABLE_EXCEPTION_CATCHING:
      opts += ['-fno-exceptions']
    for src in files:
      o = in_temp(src + '.o')
      srcfile = shared.path_from_root(src_dirname, src)
      commands.append([shared.PYTHON, shared.EMXX, srcfile, '-o', o, '-std=c++11'] + opts)
      o_s.append(o)
    run_commands(commands)
    if lib_filename.endswith('.bc'):
      shared.Building.link(o_s, in_temp(lib_filename))
    elif lib_filename.endswith('.a'):
      shared.Building.emar('cr', in_temp(lib_filename), o_s)
    else:
      raise Exception('unknown suffix ' + lib_filename)
    return in_temp(lib_filename)

  # libc
  def create_libc(libname):
    logging.debug(' building libc for cache')
    libc_files = [
    ]
    musl_srcdir = shared.path_from_root('system', 'lib', 'libc', 'musl', 'src')
    blacklist = set(
      ['ipc', 'passwd', 'thread', 'signal', 'sched', 'ipc', 'time', 'linux', 'aio', 'exit', 'legacy', 'mq', 'process', 'search', 'setjmp', 'env', 'ldso', 'conf'] + # musl modules
      ['memcpy.c', 'memset.c', 'memmove.c', 'getaddrinfo.c', 'getnameinfo.c', 'inet_addr.c', 'res_query.c', 'gai_strerror.c', 'proto.c', 'gethostbyaddr.c', 'gethostbyaddr_r.c', 'gethostbyname.c', 'gethostbyname2_r.c', 'gethostbyname_r.c', 'gethostbyname2.c', 'usleep.c', 'alarm.c', 'syscall.c', '_exit.c'] + # individual files
      ['abs.c', 'cos.c', 'cosf.c', 'cosl.c', 'sin.c', 'sinf.c', 'sinl.c', 'tan.c', 'tanf.c', 'tanl.c', 'acos.c', 'acosf.c', 'acosl.c', 'asin.c', 'asinf.c', 'asinl.c', 'atan.c', 'atanf.c', 'atanl.c', 'atan2.c', 'atan2f.c', 'atan2l.c', 'exp.c', 'expf.c', 'expl.c', 'log.c', 'logf.c', 'logl.c', 'sqrt.c', 'sqrtf.c', 'sqrtl.c', 'fabs.c', 'fabsf.c', 'fabsl.c', 'ceil.c', 'ceilf.c', 'ceill.c', 'floor.c', 'floorf.c', 'floorl.c', 'pow.c', 'powf.c', 'powl.c', 'round.c', 'roundf.c', 'rintf.c'] # individual math files
    )
    # TODO: consider using more math code from musl, doing so makes box2d faster
    for dirpath, dirnames, filenames in os.walk(musl_srcdir):
      for f in filenames:
        if f.endswith('.c'):
          if f in blacklist: continue
          dir_parts = os.path.split(dirpath)
          cancel = False
          for part in dir_parts:
            if part in blacklist:
              cancel = True
              break
          if not cancel:
            libc_files.append(os.path.join(musl_srcdir, dirpath, f))
    # Add in extra non-musl things
    libc_files.append(shared.path_from_root('system', 'lib', 'libc', 'extras.c'))
    # Without -fno-builtin, LLVM can optimize away or convert calls to library
    # functions to something else based on assumptions that they behave exactly
    # like the standard library. This can cause unexpected bugs when we use our
    # custom standard library. The same for other libc/libm builds.
    args = ['-Os', '-fno-builtin']
    if shared.Settings.USE_PTHREADS:
      args += ['-s', 'USE_PTHREADS=1']
      assert '-mt' in libname
    else:
      assert '-mt' not in libname
    return build_libc(libname, libc_files, args)

  def create_pthreads(libname):
    # Add pthread files.
    pthreads_files = files_in_path(
      path_components=['system', 'lib', 'libc', 'musl', 'src', 'thread'],
      filenames=[
        'pthread_attr_destroy.c', 'pthread_condattr_setpshared.c',
        'pthread_mutex_lock.c', 'pthread_spin_destroy.c', 'pthread_attr_get.c',
        'pthread_cond_broadcast.c', 'pthread_mutex_setprioceiling.c',
        'pthread_spin_init.c', 'pthread_attr_init.c', 'pthread_cond_destroy.c',
        'pthread_mutex_timedlock.c', 'pthread_spin_lock.c',
        'pthread_attr_setdetachstate.c', 'pthread_cond_init.c',
        'pthread_mutex_trylock.c', 'pthread_spin_trylock.c',
        'pthread_attr_setguardsize.c', 'pthread_cond_signal.c',
        'pthread_mutex_unlock.c', 'pthread_spin_unlock.c',
        'pthread_attr_setinheritsched.c', 'pthread_cond_timedwait.c',
        'pthread_once.c', 'sem_destroy.c', 'pthread_attr_setschedparam.c',
        'pthread_cond_wait.c', 'pthread_rwlockattr_destroy.c', 'sem_getvalue.c',
        'pthread_attr_setschedpolicy.c', 'pthread_equal.c', 'pthread_rwlockattr_init.c',
        'sem_init.c', 'pthread_attr_setscope.c', 'pthread_getspecific.c',
        'pthread_rwlockattr_setpshared.c', 'sem_open.c', 'pthread_attr_setstack.c',
        'pthread_key_create.c', 'pthread_rwlock_destroy.c', 'sem_post.c',
        'pthread_attr_setstacksize.c', 'pthread_mutexattr_destroy.c',
        'pthread_rwlock_init.c', 'sem_timedwait.c', 'pthread_barrierattr_destroy.c',
        'pthread_mutexattr_init.c', 'pthread_rwlock_rdlock.c', 'sem_trywait.c',
        'pthread_barrierattr_init.c', 'pthread_mutexattr_setprotocol.c',
        'pthread_rwlock_timedrdlock.c', 'sem_unlink.c',
        'pthread_barrierattr_setpshared.c', 'pthread_mutexattr_setpshared.c',
        'pthread_rwlock_timedwrlock.c', 'sem_wait.c', 'pthread_barrier_destroy.c',
        'pthread_mutexattr_setrobust.c', 'pthread_rwlock_tryrdlock.c',
        '__timedwait.c', 'pthread_barrier_init.c', 'pthread_mutexattr_settype.c',
        'pthread_rwlock_trywrlock.c', 'vmlock.c', 'pthread_barrier_wait.c',
        'pthread_mutex_consistent.c', 'pthread_rwlock_unlock.c', '__wait.c',
        'pthread_condattr_destroy.c', 'pthread_mutex_destroy.c',
        'pthread_rwlock_wrlock.c', 'pthread_condattr_init.c',
        'pthread_mutex_getprioceiling.c', 'pthread_setcanceltype.c',
        'pthread_condattr_setclock.c', 'pthread_mutex_init.c',
        'pthread_setspecific.c', 'pthread_setcancelstate.c'
      ])
    pthreads_files += [os.path.join('pthread', 'library_pthread.c')]
    return build_libc(libname, pthreads_files, ['-O2', '-s', 'USE_PTHREADS=1'])

  def create_pthreads_asmjs(libname):
    pthreads_files = [os.path.join('pthread', 'library_pthread_asmjs.c')]
    return build_libc(libname, pthreads_files, ['-O2', '-s', 'USE_PTHREADS=1'])

  def create_wasm_libc(libname):
    # in asm.js we just use Math.sin etc., which is good for code size. But
    # wasm doesn't have such builtins, so we need to bundle in more code
    files = files_in_path(
      path_components=['system', 'lib', 'libc', 'musl', 'src', 'math'],
      filenames=['cos.c', 'cosf.c', 'cosl.c', 'sin.c', 'sinf.c', 'sinl.c',
                 'tan.c', 'tanf.c', 'tanl.c', 'acos.c', 'acosf.c', 'acosl.c',
                 'asin.c', 'asinf.c', 'asinl.c', 'atan.c', 'atanf.c', 'atanl.c',
                 'atan2.c', 'atan2f.c', 'atan2l.c', 'exp.c', 'expf.c', 'expl.c',
                 'log.c', 'logf.c', 'logl.c', 'pow.c', 'powf.c', 'powl.c'])

    return build_libc(libname, files, ['-O2', '-fno-builtin'])

  # libcxx
  def create_libcxx(libname):
    logging.debug('building libcxx for cache')
    libcxx_files = [
      'algorithm.cpp',
      'any.cpp',
      'bind.cpp',
      'chrono.cpp',
      'condition_variable.cpp',
      'debug.cpp',
      'exception.cpp',
      'future.cpp',
      'hash.cpp',
      'ios.cpp',
      'iostream.cpp',
      'locale.cpp',
      'memory.cpp',
      'mutex.cpp',
      'new.cpp',
      'optional.cpp',
      'random.cpp',
      'regex.cpp',
      'shared_mutex.cpp',
      'stdexcept.cpp',
      'string.cpp',
      'strstream.cpp',
      'system_error.cpp',
      'thread.cpp',
      'typeinfo.cpp',
      'utility.cpp',
      'valarray.cpp',
      'variant.cpp'
    ]
    libcxxabi_include = shared.path_from_root('system', 'lib', 'libcxxabi', 'include')
    return build_libcxx(
      os.path.join('system', 'lib', 'libcxx'), libname, libcxx_files,
      ['-DLIBCXX_BUILDING_LIBCXXABI=1', '-D_LIBCPP_BUILDING_LIBRARY', '-Oz', '-I' + libcxxabi_include],
      has_noexcept_version=True)

  # libcxxabi - just for dynamic_cast for now
  def create_libcxxabi(libname):
    logging.debug('building libcxxabi for cache')
    libcxxabi_files = [
      'abort_message.cpp',
      'cxa_aux_runtime.cpp',
      'cxa_default_handlers.cpp',
      'cxa_demangle.cpp',
      'cxa_exception_storage.cpp',
      'cxa_guard.cpp',
      'cxa_new_delete.cpp',
      'cxa_handlers.cpp',
      'exception.cpp',
      'stdexcept.cpp',
      'typeinfo.cpp',
      'private_typeinfo.cpp'
    ]
    libcxxabi_include = shared.path_from_root('system', 'lib', 'libcxxabi', 'include')
    return build_libcxx(
      os.path.join('system', 'lib', 'libcxxabi', 'src'), libname, libcxxabi_files,
      ['-Oz', '-I' + libcxxabi_include])

  # gl
  def create_gl(libname): # libname is ignored, this is just one .o file
    o = in_temp('gl.o')
    check_call([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', 'gl.c'), '-o', o])
    return o

  # al
  def create_al(libname): # libname is ignored, this is just one .o file
    o = in_temp('al.o')
    check_call([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', 'al.c'), '-o', o, '-Os'])
    return o

  def create_html5(libname):
    src_dir = shared.path_from_root('system', 'lib', 'html5')
    files = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
      files += [os.path.join(src_dir, f) for f in filenames]
    return build_libc(libname, files, ['-Oz'])

  def create_compiler_rt(libname):
    files = files_in_path(
      path_components=['system', 'lib', 'compiler-rt', 'lib', 'builtins'],
      filenames=['divdc3.c', 'divsc3.c', 'muldc3.c', 'mulsc3.c'])

    o_s = []
    commands = []
    for src in files:
      o = in_temp(os.path.basename(src) + '.o')
      commands.append([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', src), '-O2', '-o', o])
      o_s.append(o)
    run_commands(commands)
    shared.Building.emar('cr', in_temp(libname), o_s)
    return in_temp(libname)

  # decides which malloc to use, and returns the source for malloc and the full library name
  def malloc_decision():
    if shared.Settings.MALLOC == 'dlmalloc':
      base = 'dlmalloc'
    elif shared.Settings.MALLOC == 'emmalloc':
      base = 'emmalloc'
    else:
      raise Exception('malloc must be one of "emmalloc", "dlmalloc", see settings.js')
    # only dlmalloc supports most modes
    def require_dlmalloc(what):
      if base != 'dlmalloc':
        logging.error('only dlmalloc is possible when using %s' % what)
        import sys
        sys.exit(1)
    extra = ''
    if shared.Settings.USE_PTHREADS:
      extra += '_threadsafe'
      require_dlmalloc('pthreads')
    if shared.Settings.EMSCRIPTEN_TRACING:
      extra += '_tracing'
      require_dlmalloc('tracing')
    if shared.Settings.SPLIT_MEMORY:
      extra += '_split'
      require_dlmalloc('split memory')
    if shared.Settings.DEBUG_LEVEL >= 3:
      extra += '_debug'
    if base == 'dlmalloc':
      source = 'dlmalloc.c'
    elif base == 'emmalloc':
      source = 'emmalloc.cpp'
    return (source, base + extra)

  def malloc_source():
    return malloc_decision()[0]

  def malloc_name():
    return malloc_decision()[1]

  def create_malloc(out_name):
    o = in_temp(out_name)
    cflags = ['-O2', '-fno-builtin']
    if shared.Settings.USE_PTHREADS:
      cflags += ['-s', 'USE_PTHREADS=1']
    if shared.Settings.EMSCRIPTEN_TRACING:
      cflags += ['--tracing']
    if shared.Settings.SPLIT_MEMORY:
      cflags += ['-DMSPACES', '-DONLY_MSPACES']
    if shared.Settings.DEBUG_LEVEL >= 3:
      cflags += ['-UNDEBUG', '-DDLMALLOC_DEBUG']
      # TODO: consider adding -DEMMALLOC_DEBUG, but that is quite slow
    else:
      cflags += ['-DNDEBUG']
    check_call([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', malloc_source()), '-o', o] + cflags)
    if shared.Settings.SPLIT_MEMORY:
      split_malloc_o = in_temp('sm' + out_name)
      check_call([shared.PYTHON, shared.EMCC, shared.path_from_root('system', 'lib', 'split_malloc.cpp'), '-o', split_malloc_o, '-O2'])
      lib = in_temp('lib' + out_name)
      shared.Building.link([o, split_malloc_o], lib)
      shutil.move(lib, o)
    return o

  def create_wasm_rt_lib(libname, files):
    o_s = []
    commands = []
    # Output should be an object file with lld, otherwise text assembly
    output_flag = '-c' if shared.Settings.EXPERIMENTAL_USE_LLD else '-S'
    for src in files:
      o = in_temp(os.path.basename(src) + '.o')
      # Use clang directly instead of emcc. Since emcc's intermediate format (produced by -S) is LLVM IR, there's no way to
      # get emcc to output wasm .s files, which is what we archive in compiler_rt.
      commands.append([
        shared.CLANG_CC,
        '--target={}'.format(shared.WASM_TARGET),
        '-mthread-model', 'single',
        output_flag,
        shared.path_from_root('system', 'lib', src),
        '-O2', '-fno-builtin', '-o', o] +
        musl_internal_includes() +
        shared.EMSDK_OPTS)
      o_s.append(o)
    run_commands(commands)
    lib = in_temp(libname)
    run_commands([[shared.LLVM_AR, 'cr', '-format=gnu', lib] + o_s])
    return lib

  def create_wasm_compiler_rt(libname):
    files = files_in_path(
      path_components=['system', 'lib', 'compiler-rt', 'lib', 'builtins'],
      filenames=['addtf3.c', 'ashlti3.c', 'ashrti3.c', 'atomic.c', 'comparetf2.c',
                 'divtf3.c', 'divti3.c', 'udivmodti4.c',
                 'extenddftf2.c', 'extendsftf2.c',
                 'fixdfti.c', 'fixsfti.c', 'fixtfdi.c', 'fixtfsi.c', 'fixtfti.c',
                 'fixunsdfti.c', 'fixunssfti.c', 'fixunstfdi.c', 'fixunstfsi.c', 'fixunstfti.c',
                 'floatditf.c', 'floatsitf.c', 'floattidf.c', 'floattisf.c',
                 'floatunditf.c', 'floatunsitf.c', 'floatuntidf.c', 'floatuntisf.c', 'lshrti3.c',
                 'modti3.c', 'multf3.c', 'multi3.c', 'subtf3.c', 'udivti3.c', 'umodti3.c', 'ashrdi3.c',
                 'ashldi3.c', 'fixdfdi.c', 'floatdidf.c', 'lshrdi3.c', 'moddi3.c',
                 'trunctfdf2.c', 'trunctfsf2.c', 'umoddi3.c', 'fixunsdfdi.c', 'muldi3.c',
                 'divdi3.c', 'divmoddi4.c', 'udivdi3.c', 'udivmoddi4.c'])
    return create_wasm_rt_lib(libname, files)

  def create_wasm_libc_rt(libname):
    # Static linking is tricky with LLVM, since e.g. memset might not be used from libc,
    # but be used as an intrinsic, and codegen will generate a libc call from that intrinsic
    # *after* static linking would have thought it is all in there. In asm.js this is not an
    # issue as we do JS linking anyhow, and have asm.js-optimized versions of all the LLVM
    # intrinsics. But for wasm, we need a better solution. For now, make another archive
    # that gets included at the same time as compiler-rt.
    math_files = files_in_path(
      path_components=['system', 'lib', 'libc', 'musl', 'src', 'math'],
      filenames=[
        'fmin.c', 'fminf.c', 'fminl.c',
        'fmax.c', 'fmaxf.c', 'fmaxl.c',
        'fmod.c', 'fmodf.c', 'fmodl.c',
        'log2.c', 'log2f.c', 'log10.c', 'log10f.c',
        'exp2.c', 'exp2f.c', 'exp10.c', 'exp10f.c',
        'scalbn.c',
      ])
    string_files = files_in_path(
      path_components=['system', 'lib', 'libc', 'musl', 'src', 'string'],
      filenames=['memcpy.c', 'memset.c', 'memmove.c'])
    return create_wasm_rt_lib(libname, math_files + string_files)

  # Setting this in the environment will avoid checking dependencies and make building big projects a little faster
  # 1 means include everything; otherwise it can be the name of a lib (libcxx, etc.)
  # You can provide 1 to include everything, or a comma-separated list with the ones you want
  force = os.environ.get('EMCC_FORCE_STDLIBS')
  force_all = force == '1'
  force = set((force.split(',') if force else []) + forced)
  if force: logging.debug('forcing stdlibs: ' + str(force))

  # Setting this will only use the forced libs in EMCC_FORCE_STDLIBS. This avoids spending time checking
  # for unresolved symbols in your project files, which can speed up linking, but if you do not have
  # the proper list of actually needed libraries, errors can occur. See below for how we must
  # export all the symbols in deps_info when using this option.
  only_forced = os.environ.get('EMCC_ONLY_FORCED_STDLIBS')
  if only_forced:
    temp_files = []

  # Add in some hacks for js libraries. If a js lib depends on a symbol provided by a C library, it must be
  # added to here, because our deps go only one way (each library here is checked, then we check the next
  # in order - libcxx, libcxextra, etc. - and then we run the JS compiler and provide extra symbols from
  # library*.js files. But we cannot then go back to the C libraries if a new dep was added!
  # TODO: Move all __deps from src/library*.js to deps_info.json, and use that single source of info
  #       both here and in the JS compiler.
  deps_info = json.loads(open(shared.path_from_root('src', 'deps_info.json')).read())
  added = set()
  def add_back_deps(need):
    more = False
    for ident, deps in deps_info.items():
      if ident in need.undefs and not ident in added:
        added.add(ident)
        more = True
        for dep in deps:
          need.undefs.add(dep)
          shared.Settings.EXPORTED_FUNCTIONS.append('_' + dep)
    if more:
      add_back_deps(need) # recurse to get deps of deps

  # Scan symbols
  symbolses = shared.Building.parallel_llvm_nm(list(map(os.path.abspath, temp_files)))

  if len(symbolses) == 0:
    class Dummy(object):
      defs = set()
      undefs = set()
    symbolses.append(Dummy())

  # depend on exported functions
  for export in shared.Settings.EXPORTED_FUNCTIONS:
    if shared.Settings.VERBOSE: logging.debug('adding dependency on export %s' % export)
    symbolses[0].undefs.add(export[1:])

  for symbols in symbolses:
    add_back_deps(symbols)

  # If we are only doing forced stdlibs, then we don't know the actual symbols we need,
  # and must assume all of deps_info must be exported. Note that this might cause
  # warnings on exports that do not exist.
  if only_forced:
    for key, value in deps_info.items():
      for dep in value:
        shared.Settings.EXPORTED_FUNCTIONS.append('_' + dep)

  system_libs = [('libcxx',        'a',  create_libcxx,      libcxx_symbols,      ['libcxxabi'], True),
                 ('libcxxabi',     'bc', create_libcxxabi,   libcxxabi_symbols,   ['libc'],      False),
                 ('gl',            'bc', create_gl,          gl_symbols,          ['libc'],      False),
                 ('al',            'bc', create_al,          al_symbols,          ['libc'],      False),
                 ('html5',         'bc', create_html5,       html5_symbols,       ['html5'],     False),
                 ('compiler-rt',   'a',  create_compiler_rt, compiler_rt_symbols, ['libc'],      False),
                 (malloc_name(),   'bc', create_malloc,      [],                  [],            False)]

  if shared.Settings.USE_PTHREADS:
    system_libs += [('libc-mt',        'bc', create_libc,           libc_symbols,     [],       False),
                    ('pthreads',       'bc', create_pthreads,       pthreads_symbols, ['libc'], False),
                    ('pthreads_asmjs', 'bc', create_pthreads_asmjs, asmjs_pthreads_symbols, ['libc'], False)]
    force.add('pthreads')
    force.add('pthreads_asmjs')
  else:
    system_libs += [('libc', 'bc', create_libc, libc_symbols, [], False)]

  force.add(malloc_name())

  # if building to wasm, we need more math code, since we have less builtins
  if shared.Settings.WASM:
    system_libs += [('wasm-libc', 'bc', create_wasm_libc, wasm_libc_symbols, [], False)]
    # if libc is included, we definitely must be, as it might need us
    for data in system_libs:
      if data[3] == libc_symbols:
        data[4].append('wasm-libc')
        break
    else:
      raise Exception('did not find libc?')

  # Go over libraries to figure out which we must include
  def maybe_noexcept(name):
    if shared.Settings.DISABLE_EXCEPTION_CATCHING:
      name += '_noexcept'
    return name
  ret = []
  has = need = None

  for shortname, suffix, create, library_symbols, deps, can_noexcept in system_libs:
    force_this = force_all or shortname in force
    if can_noexcept: shortname = maybe_noexcept(shortname)
    if force_this:
      suffix = 'bc' # .a files do not always link in all their parts; don't use them when forced
    name = shortname + '.' + suffix

    if not force_this:
      need = set()
      has = set()
      for symbols in symbolses:
        if shared.Settings.VERBOSE: logging.debug('undefs: ' + str(symbols.undefs))
        for library_symbol in library_symbols:
          if library_symbol in symbols.undefs:
            need.add(library_symbol)
          if library_symbol in symbols.defs:
            has.add(library_symbol)
      for haz in has: # remove symbols that are supplied by another of the inputs
        if haz in need:
          need.remove(haz)
      if shared.Settings.VERBOSE: logging.debug('considering %s: we need %s and have %s' % (name, str(need), str(has)))
    if force_this or (len(need) > 0 and not only_forced):
      # We need to build and link the library in
      logging.debug('including %s' % name)
      def do_create():
        ret = create(name)
        return ret
      libfile = shared.Cache.get(name, do_create, extension=suffix)
      ret.append(libfile)
      force = force.union(deps)
  ret.sort(key=lambda x: x.endswith('.a')) # make sure to put .a files at the end.

  # Handle backend compiler_rt separately because it is not a bitcode system lib like the others.
  # Here, just ensure that it's in the cache.
  if shared.Settings.WASM and shared.Settings.WASM_BACKEND:
    shared.Cache.get('wasm_compiler_rt.a', lambda: create_wasm_compiler_rt('wasm_compiler_rt.a'), extension='a')
    shared.Cache.get('wasm_libc_rt.a', lambda: create_wasm_libc_rt('wasm_libc_rt.a'), extension='a')

  for actual in ret:
    if os.path.basename(actual) == 'libcxxabi.bc':
      # libcxxabi and libcxx *static* linking is tricky. e.g. cxa_demangle.cpp disables c++
      # exceptions, but since the string methods in the headers are *weakly* linked, then
      # we might have exception-supporting versions of them from elsewhere, and if libcxxabi
      # is first then it would "win", breaking exception throwing from those string
      # header methods. To avoid that, we link libcxxabi last.
      ret = [f for f in ret if f != actual] + [actual]

  return ret

#---------------------------------------------------------------------------
# emscripten-ports library management (https://github.com/emscripten-ports)
#---------------------------------------------------------------------------

from . import ports

class Ports(object):
  @staticmethod
  def build_port(src_path, output_path, includes=[], flags=[], exclude_files=[], exclude_dirs=[]):
      srcs = []
      for root, dirs, files in os.walk(src_path, topdown=False):
        if any((excluded in root) for excluded in exclude_dirs):
          continue
        for file in files:
            if (file.endswith('.c') or file.endswith('.cpp')) and not any((excluded in file) for excluded in exclude_files):
                srcs.append(os.path.join(root, file))
      include_commands = ['-I' + src_path ]
      for include in includes:
          include_commands.append('-I' + include)

      commands = []
      objects = []
      for src in srcs:
        obj = src + '.o'
        commands.append([shared.PYTHON, shared.EMCC, src, '-O2', '-o', obj, '-w'] + include_commands + flags)
        objects.append(obj)

      run_commands(commands)
      shared.Building.link(objects, output_path)

  @staticmethod
  def run_commands(commands): # make easily available for port objects
    run_commands(commands)

  @staticmethod
  def get_dir():
    dirname = os.environ.get('EM_PORTS') or os.path.expanduser(os.path.join('~', '.emscripten_ports'))
    shared.safe_ensure_dirs(dirname)
    return dirname

  @staticmethod
  def erase():
    dirname = Ports.get_dir()
    shared.try_delete(dirname)
    if os.path.exists(dirname):
      logging.warning('could not delete ports dir %s - try to delete it manually' % dirname)

  @staticmethod
  def get_build_dir():
    return shared.Cache.get_path('ports-builds')

  name_cache = set()

  @staticmethod
  def fetch_project(name, url, subdir, is_tarbz2=False):
    fullname = os.path.join(Ports.get_dir(), name)

    if name not in Ports.name_cache: # only mention each port once in log
      logging.debug('including port: ' + name)
      logging.debug('    (at ' + fullname + ')')
      Ports.name_cache.add(name)

    class State(object):
      retrieved = False
      unpacked = False

    def retrieve():
      # if EMCC_LOCAL_PORTS is set, we use a local directory as our ports. This is useful
      # for testing. This env var should be in format
      #     name=dir|tag,name=dir|tag
      # e.g.
      #     sdl2=/home/username/dev/ports/SDL2|SDL2-master
      # so you could run
      #     EMCC_LOCAL_PORTS="sdl2=/home/alon/Dev/ports/SDL2|SDL2-master" ./tests/runner.py browser.test_sdl2_mouse
      # note that tag **must** be the tag in sdl.py, it is where we store to (not where we load from, we just load the local dir)
      local_ports = os.environ.get('EMCC_LOCAL_PORTS')
      if local_ports:
        local_ports = [pair.split('=', 1) for pair in local_ports.split(',')]
        for local in local_ports:
          if name == local[0]:
            path, subdir = local[1].split('|')
            logging.warning('grabbing local port: ' + name + ' from ' + path + ', into ' + subdir)
            # zip up the directory, so it looks the same as if we downloaded a zip from the remote server
            z = zipfile.ZipFile(fullname + '.zip', 'w')
            def add_dir(p):
              for f in os.listdir(p):
                full = os.path.join(p, f)
                if os.path.isdir(full):
                  add_dir(full)
                else:
                  if not f.startswith('.'): # ignore hidden files, including .git/ etc.
                    z.write(full, os.path.join(subdir, os.path.relpath(full, path)))
            add_dir(path)
            z.close()
            State.retrieved = True
            return
      # retrieve from remote server
      logging.warning('retrieving port: ' + name + ' from ' + url)
      try:
        from urllib.request import urlopen
      except ImportError:
        # Python 2 compatibility
        from urllib2 import urlopen
      f = urlopen(url)
      data = f.read()
      open(fullname + ('.zip' if not is_tarbz2 else '.tar.bz2'), 'wb').write(data)
      State.retrieved = True

    def check_tag():
      if is_tarbz2:
        names = tarfile.open(fullname + '.tar.bz2', 'r:bz2').getnames()
      else:
        names = zipfile.ZipFile(fullname + '.zip', 'r').namelist()

      # check if first entry of the archive is prefixed with the same
      # tag as we need so no longer download and recompile if so
      return bool(re.match(subdir + r'(\\|/|$)', names[0]))

    def unpack():
      logging.warning('unpacking port: ' + name)
      shared.safe_ensure_dirs(fullname)
      if is_tarbz2:
        z = tarfile.open(fullname + '.tar.bz2', 'r:bz2')
      else:
        z = zipfile.ZipFile(fullname + '.zip', 'r')
      try:
        cwd = os.getcwd()
        os.chdir(fullname)
        z.extractall()
      finally:
        os.chdir(cwd)
      State.unpacked = True

    # main logic. do this under a cache lock, since we don't want multiple jobs to
    # retrieve the same port at once

    shared.Cache.acquire_cache_lock()
    try:
      if not os.path.exists(fullname + '.zip'):
        retrieve()

      if not os.path.exists(fullname):
        unpack()

      if not check_tag():
        logging.warning('local copy of port is not correct, retrieving from remote server')
        shared.try_delete(fullname)
        shared.try_delete(fullname + '.zip')
        retrieve()
        unpack()

      if State.unpacked:
        # we unpacked a new version, clear the build in the cache
        Ports.clear_project_build(name)
    finally:
      shared.Cache.release_cache_lock()

  @staticmethod
  def build_project(name, subdir, configure, generated_libs, post_create=None):
    def create():
      logging.info('building port: ' + name + '...')
      port_build_dir = Ports.get_build_dir()
      shared.safe_ensure_dirs(port_build_dir)
      libs = shared.Building.build_library(name, port_build_dir, None, generated_libs, source_dir=os.path.join(Ports.get_dir(), name, subdir), copy_project=True,
                                           configure=configure, make=['make', '-j' + str(CORES)])
      assert len(libs) == 1
      if post_create: post_create()
      return libs[0]
    return shared.Cache.get(name, create)

  @staticmethod
  def clear_project_build(name):
    shared.try_delete(os.path.join(Ports.get_build_dir(), name))
    shared.try_delete(shared.Cache.get_path(name + '.bc'))

  @staticmethod
  def build_native(subdir):
    shared.Building.ensure_no_emmake('We cannot build the native system library in "%s" when under the influence of emmake/emconfigure. To avoid this, create system dirs beforehand, so they are not auto-built on demand. For example, for binaryen, do "python embuilder.py build binaryen"' % subdir)

    old = os.getcwd()

    try:
      os.chdir(subdir)

      cmake_build_type = 'Release'

      # Configure
      subprocess.check_call(['cmake', '-DCMAKE_BUILD_TYPE=' + cmake_build_type, '.'])

      # Check which CMake generator CMake used so we know which form to pass parameters to make/msbuild/etc. build tool.
      generator = re.search('CMAKE_GENERATOR:INTERNAL=(.*)$', open('CMakeCache.txt', 'r').read(), re.MULTILINE).group(1)

      # Make variants support '-jX' for number of cores to build, MSBuild does /maxcpucount:X
      num_cores = os.environ.get('EMCC_CORES') or str(multiprocessing.cpu_count())
      make_args = []
      if 'Makefiles' in generator and not 'NMake' in generator: make_args = ['--', '-j', num_cores]
      elif 'Visual Studio' in generator: make_args = ['--config', cmake_build_type, '--', '/maxcpucount:' + num_cores]

      # Kick off the build.
      subprocess.check_call(['cmake', '--build', '.'] + make_args)
    finally:
      os.chdir(old)

# get all ports
def get_ports(settings):
  ret = []

  ok = False
  try:
    process_dependencies(settings)
    for port in ports.ports:
      # ports return their output files, which will be linked, or a txt file
      ret += [f for f in port.get(Ports, settings, shared) if not f.endswith('.txt')]
    ok = True
  finally:
    if not ok:
      logging.error('a problem occurred when using an emscripten-ports library. try to run    emcc --clear-ports    and then run this command again')

  ret.reverse()
  return ret

def process_dependencies(settings):
  for port in reversed(ports.ports):
    if hasattr(port, "process_dependencies"):
      port.process_dependencies(settings)

def process_args(args, settings):
  process_dependencies(settings)
  for port in ports.ports:
    args = port.process_args(Ports, args, settings, shared)
  return args

# get a single port
def get_port(name, settings):
  port = ports.ports_by_name[name]
  if hasattr(port, "process_dependencies"):
    port.process_dependencies(settings)
  # ports return their output files, which will be linked, or a txt file
  return [f for f in port.get(Ports, settings, shared) if not f.endswith('.txt')]

def show_ports():
  print('Available ports:')
  for port in ports.ports:
    print('   ', port.show())
