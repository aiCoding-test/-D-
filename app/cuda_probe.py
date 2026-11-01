"""Bounded CUDA Driver API integer kernel; no model, toolkit, or large dependency.

The parent sets CUDA_VISIBLE_DEVICES BEFORE this module initializes the driver.
Each kernel is small and synchronized; process cancellation releases its context.
"""
import ctypes as C
import hashlib
import os
import time
import uuid

PTX=b'''.version 7.0
.target sm_70
.address_size 64
.visible .entry probe(.param .u64 dst,.param .u32 n,.param .u32 seed) {
 .reg .pred %p; .reg .b32 %r<8>; .reg .b64 %rd<4>;
 ld.param.u64 %rd0,[dst]; ld.param.u32 %r0,[n]; ld.param.u32 %r1,[seed];
 mov.u32 %r2,%tid.x; mov.u32 %r3,%ctaid.x; mov.u32 %r4,%ntid.x;
 mad.lo.u32 %r5,%r3,%r4,%r2; setp.ge.u32 %p,%r5,%r0; @%p bra DONE;
 mul.wide.u32 %rd1,%r5,4; add.u64 %rd2,%rd0,%rd1;
 add.u32 %r6,%r5,%r1; st.global.u32 [%rd2],%r6;
 DONE: ret;
}
'''
PTX_SHA=hashlib.sha256(PTX).hexdigest()


class CudaError(Exception):
    def __init__(self,code,operation,detail):
        self.code,self.operation,self.detail=code,operation,detail
        super().__init__(f'{operation}: CUDA {code}: {detail}')


class Probe:
    def __init__(self,physical_uuid,memory_mb,seed):
        self.lib=C.CDLL('libcuda.so.1');self.ctx=C.c_void_p();self.ptr=C.c_uint64();self.module=C.c_void_p()
        self.uuid=physical_uuid;self.bytes=memory_mb*1048576;self.seed=seed;self.launches=0
        self.call('cuInit',0)
        count=C.c_int();self.call('cuDeviceGetCount',C.byref(count))
        if count.value!=1:raise RuntimeError('Expected exactly one visible CUDA device')
        device=C.c_int();self.call('cuDeviceGet',C.byref(device),0)
        raw=(C.c_ubyte*16)();self.call('cuDeviceGetUuid_v2',C.byref(raw),device)
        actual='GPU-'+str(uuid.UUID(bytes=bytes(raw)))
        if actual.lower()!=physical_uuid.lower():raise RuntimeError(f'GPU_BINDING_MISMATCH {actual} != {physical_uuid}')
        self.driver=C.c_int();self.call('cuDriverGetVersion',C.byref(self.driver))
        self.call('cuCtxCreate_v2',C.byref(self.ctx),0,device)
        self.call('cuMemAlloc_v2',C.byref(self.ptr),C.c_size_t(self.bytes))
        self.call('cuModuleLoadData',C.byref(self.module),C.c_char_p(PTX))
        self.fn=C.c_void_p();self.call('cuModuleGetFunction',C.byref(self.fn),self.module,b'probe')
        print(f'CUDA_BOUND physical_uuid={actual} ordinal=0 driver={self.driver.value} bytes={self.bytes} ptx={PTX_SHA}',flush=True)

    def call(self,name,*args):
        code=getattr(self.lib,name)(*args)
        if code:
            message=C.c_char_p();self.lib.cuGetErrorString(code,C.byref(message))
            raise CudaError(code,name,message.value.decode() if message.value else 'unavailable')

    def compute(self,seconds):
        n=C.c_uint(self.bytes//4);seed=C.c_uint(self.seed)
        args=(C.c_void_p*3)(C.addressof(self.ptr),C.addressof(n),C.addressof(seed))
        end=time.monotonic()+seconds
        while True:
            self.call('cuLaunchKernel',self.fn,(n.value+255)//256,1,1,256,1,1,0,None,args,None)
            self.call('cuCtxSynchronize');self.launches+=1
            if time.monotonic()>=end:break
            time.sleep(.01)  # Bound duty cycle, never benchmark or monopolize desktop GPU.
        result=(C.c_uint*64)()
        self.call('cuMemcpyDtoH_v2',C.byref(result),self.ptr,C.c_size_t(C.sizeof(result)))
        assert list(result)==[(i+self.seed)%(2**32) for i in range(64)],'CUDA output validation failed'
        return {'physical_uuid':self.uuid,'cuda_ordinal':0,'driver_api':self.driver.value,
            'bytes':self.bytes,'ptx_sha256':PTX_SHA,'kernel_launches':self.launches,
            'checksum_first64':sum(result),'validated_values':64,'pid':os.getpid()}

    def bounded_oom(self):
        """Real driver OOM within a NEW 16 MiB pool, not whole-card exhaustion."""
        class Location(C.Structure):_fields_=[('type',C.c_int),('id',C.c_int)]
        class Props(C.Structure):
            _fields_=[('allocType',C.c_int),('handleTypes',C.c_int),('location',Location),
                ('win32SecurityAttributes',C.c_void_p),('maxSize',C.c_size_t),('usage',C.c_ushort),('reserved',C.c_ubyte*54)]
        props=Props();props.allocType=1;props.location.type=1;props.location.id=0;props.maxSize=16*1048576
        pool=C.c_void_p();stream=C.c_void_p();ptr=C.c_uint64()
        self.call('cuMemPoolCreate',C.byref(pool),C.byref(props))
        try:
            self.call('cuStreamCreate',C.byref(stream),1)
            try:
                print('CUDA_OOM_TEST pool_max_bytes=16777216 request_bytes=67108864',flush=True)
                self.call('cuMemAllocFromPoolAsync',C.byref(ptr),C.c_size_t(64*1048576),pool,stream)
                self.call('cuStreamSynchronize',stream)
                self.call('cuMemFreeAsync',ptr,stream)
                self.call('cuStreamSynchronize',stream)
                raise RuntimeError('OOM_TEST_UNSUPPORTED: pool cap was not enforced; no escalation')
            finally:self.lib.cuStreamDestroy_v2(stream)
        finally:self.lib.cuMemPoolDestroy(pool)

    def close(self):
        if self.ctx:self.lib.cuCtxDestroy_v2(self.ctx);self.ctx=None
