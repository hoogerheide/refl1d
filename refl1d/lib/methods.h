/* This program is public domain. */

#define REF(obj,name  ) PyObject_GetAttrString(obj,#name)
#define SET(obj,name,v) PyObject_SetAttrString(obj,#name,v)

// TODO: can we free the view before using the pointer with writable vectors?
/* ***
// Vector binding glue
#if (PY_VERSION_HEX > 0x03000000) && !defined(Py_LIMITED_API)
  // Assuming that a view into a writable vector points to a 
  // non-changing pointer for the duration of the C call, capture 
  // the view pointer and immediately free the view.
  #define VECTOR(VEC_obj, VEC_buf, VEC_len) do { \
    Py_buffer VEC_view; \
    int VEC_err = PyObject_GetBuffer(VEC_obj, &VEC_view, PyBUF_WRITABLE|PyBUF_FORMAT); \
    if (VEC_err < 0 || sizeof(*VEC_buf) != VEC_view.itemsize) return NULL; \
    VEC_buf = (typeof(VEC_buf))VEC_view.buf; \
    VEC_len = VEC_view.len/sizeof(*VEC_buf); \
    PyBuffer_Release(&VEC_view); \
  } while (0)
#else
  #define VECTOR(VEC_obj, VEC_buf, VEC_len) do { \
    int VEC_err = PyObject_AsWriteBuffer(VEC_obj, (void **)(&VEC_buf), &VEC_len); \
    if (VEC_err < 0) return NULL; \
    VEC_len /= sizeof(*VEC_buf); \
  } while (0)
#endif
*** */

// Vector binding glue
#if 0 && (PY_VERSION_HEX > 0x03000000) && !defined(Py_LIMITED_API)

  // Buffer protocol for python 3 requires freeing each view
  // that is retrieved, so need to leave space to retrieve
  // the buffer view for each vector and free them after they
  // have been used.  This is particularly problematic if there
  // is an error discovered while interpreting the inputs, since
  // we may be freeing a subset.
  #define DECLARE_VECTORS(VEC_n) \
   Py_buffer VEC_views[VEC_n]; \
   int VEC_current = 0; \
   const int VEC_maximum = VEC_n;

  #define FREE_VECTORS() \
   do { \
       int VEC_k = 0; \
       while (VEC_k < VEC_current) PyBuffer_Release(&VEC_views[VEC_k++]); \
    } while (0)

  #define _VECTOR(VEC_obj, VEC_buf, VEC_len, VEC_flags) \
   do { \
       if (VEC_current >= VEC_maximum) { \
           PyErr_SetString(PyExc_TypeError, "not enough vectors declared"); \
           FREE_VECTORS(); \
           return NULL; \
       } else { \
           Py_buffer *VEC_view = &VEC_views[VEC_current]; \
           int VEC_err = PyObject_GetBuffer(VEC_obj, VEC_view, VEC_flags); \
           if (VEC_err < 0) { FREE_VECTORS(); return NULL; } \
           VEC_current++; \
           if (sizeof(*VEC_buf) != VEC_view->itemsize) { \
               PyErr_SetString(PyExc_TypeError, "wrong numeric type for vector"); \
               FREE_VECTORS(); \
               return NULL; \
           } \
           VEC_buf = (typeof(VEC_buf))VEC_view->buf; \
           VEC_len = VEC_view->len/sizeof(*VEC_buf); \
       } \
   } while (0)

  #define INVECTOR(obj, buf, len) _VECTOR(obj, buf, len, PyBUF_SIMPLE|PyBUF_FORMAT)
  #define OUTVECTOR(obj, buf, len) _VECTOR(obj, buf, len, PyBUF_WRITABLE|PyBUF_FORMAT)

#else

  #define DECLARE_VECTORS(n) do {} while(0)
  #define FREE_VECTORS() do {} while(0)

  #define INVECTOR(obj,buf,len) \
    do { \
        int err = PyObject_AsReadBuffer(obj, (const void **)(&buf), &len); \
        if (err < 0) return NULL; \
        len /= sizeof(*buf); \
    } while (0)

  #define OUTVECTOR(obj,buf,len) \
    do { \
        int err = PyObject_AsWriteBuffer(obj, (void **)(&buf), &len); \
        if (err < 0) return NULL; \
        len /= sizeof(*buf); \
    } while (0)

#endif

#define SCALAR(obj) PyFloat_AsDouble(obj)


//PyObject* pyvector(int n, double v[]);

PyObject* Preflectivity_amplitude(PyObject*obj,PyObject*args);
PyObject* Pmagnetic_amplitude(PyObject* obj, PyObject* args);
PyObject* Pcalculate_u1_u3(PyObject* obj, PyObject* args);
PyObject* Palign_magnetic(PyObject *obj, PyObject *args);
PyObject* Pcontract_by_step(PyObject*obj,PyObject*args);
PyObject* Pcontract_by_area(PyObject*obj,PyObject*args);
PyObject* Pcontract_mag(PyObject*obj,PyObject*args);
PyObject* Pconvolve(PyObject*obj,PyObject*args);
PyObject* Pconvolve_sampled(PyObject*obj,PyObject*args);
