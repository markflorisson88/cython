import os
import copy

from Cython import Utils
from Cython.Compiler import (ExprNodes, Nodes, PyrexTypes, Visitor,
                             Code, Naming, MemoryView, Errors, UtilNodes,
                             UtilityCode)
from Cython.Compiler.Errors import error, CompileError

from Cython.minivect import miniast
from Cython.minivect import minitypes
from Cython.minivect import miniutils
from Cython.minivect import minierror
from Cython.minivect import codegen
from Cython.minivect import specializers
from Cython.minivect import graphviz

_debug = True
_context_debug = False

#
### Graphviz related things. .dot files are only written when _debug is true
#
graphviz_counter = 0
graphviz_out_filename_unspecialized = os.path.expanduser("~/ast.dot")
graphviz_out_filename = os.path.expanduser("~/ast%d.dot")


# Macro that should be defined to enable explicit vectorization
cython_vector_size = "CYTHON_VECTOR_SIZE"

class TypeMapper(minitypes.TypeMapper):
    def map_type(self, type, wrap=False):
        if type.is_typedef:
            return minitypes.TypeWrapper(type, self.context)
        elif type.is_memoryviewslice:
            dtype = self.map_type(type.dtype, wrap=wrap)
            return minitypes.ArrayType(dtype, len(type.axes),
                                       is_c_contig=type.is_c_contig,
                                       is_f_contig=type.is_f_contig)
        elif type.is_float:
            if type == PyrexTypes.c_float_type:
                return minitypes.float_
            elif type == PyrexTypes.c_double_type:
                return minitypes.double
            elif type == PyrexTypes.c_longdouble_type:
                return minitypes.longdouble
        elif type.is_int:
            if type == PyrexTypes.c_char_type:
                return minitypes.char
            elif type == PyrexTypes.c_int_type:
                signed_types = [minitypes.int8, minitypes.short,
                                minitypes.int_, minitypes.long_,
                                minitypes.longlong]
                unsigned_types = [minitypes.uint8, minitypes.ushort,
                                  minitypes.uint, minitypes.ulong,
                                  minitypes.ulonglong]
                if type.signed:
                    return signed_types[type.rank]
                else:
                    return unsigned_types[type.rank]
        elif type.is_pyobject:
            return minitypes.object_

        if wrap:
            return minitypes.TypeWrapper(type, self.context)
        else:
            raise minierror.UnmappableTypeError(type)

class CythonSpecializerMixin(object):
    is_partial_mapping = False
    has_error_handler = False

    def visit_FunctionNode(self, node):
        b = self.astbuilder

        def qualify(type):
            type = type.qualify("const", "CYTHON_RESTRICT")
            type.base_type = type.base_type.qualify("const")
            return type

        node.shape.type = qualify(node.shape.type)
        for idx, arg in enumerate(arg for arg in node.arguments
                                          if arg.is_array_funcarg):
            if idx > 0:
                arg.data_pointer.type = qualify(arg.data_pointer.type)
            else:
                arg.data_pointer.type = arg.data_pointer.type.qualify(
                                            "const", "CYTHON_RESTRICT")
            if arg.strides_pointer:
                arg.strides_pointer.type = qualify(arg.strides_pointer.type)

        type = minitypes.Py_ssize_t.qualify("const")
        if self.is_tiled_specializer:
            self._blocksize_var = b.variable(type, 'blocksize')
            node.scalar_arguments.append(b.funcarg(self._blocksize_var))

        node.omp_size = b.variable(type, 'omp_size')
        node.scalar_arguments.append(b.funcarg(node.omp_size))
        node = super(CythonSpecializerMixin, self).visit_FunctionNode(node)
        return node

    def visit_NodeWrapper(self, node):
        self.is_partial_mapping = True
        for op in node.operands:
            op.variable = self.visit(op.variable)
        return node

    def visit_ErrorHandler(self, node):
        self.has_error_handler = True
        return super(CythonSpecializerMixin, self).visit_ErrorHandler(node)

    def visit_OpenMPLoopNode(self, node):
        self.visitchildren(node)
        if self.has_error_handler:
            # In case of any potential gotos, don't use OpenMP loops
            return node.for_node
        return node

    def get_blocksize(self):
        return self._blocksize_var

def create_hybrid_code(codegen, old_minicode):
    minicode = codegen.context.codewriter_cls(codegen.context)
    minicode.indent = old_minicode.indent
    code = CythonCCodeWriter(codegen.context, minicode)
    code.level = minicode.indent
    code.declaration_levels = list(old_minicode.declaration_levels)
    code.codegen = codegen.clone(codegen.context, code)
    return code

class CCodeGen(codegen.VectorCodegen):

    def __init__(self, context, codewriter):
        super(CCodeGen, self).__init__(context, codewriter)
        self.error_handlers = []

    def visit_ErrorHandler(self, node):
        self.error_handlers.append(node)
        result = super(CCodeGen, self).visit_ErrorHandler(node)
        self.error_handlers.pop()
        return result

    def visit_FunctionNode(self, node):
        result = super(CCodeGen, self).visit_FunctionNode(node)
        if self.specializer.is_partial_mapping:
            self.code.function_declarations.putln("__Pyx_RefNannyDeclarations")
            self.code.before_loop.putln(
                    '__Pyx_RefNannySetupContext("%s", 1);' % node.mangled_name)

    def visit_NodeWrapper(self, node):
        for operand in node.operands:
            operand.codegen = self

        node = node.opaque_node
        code = create_hybrid_code(self, self.code)

        # create funcstate and evaluate the expression
        code.enter_cfunc_scope()
        node.generate_evaluation_code(code)
        if node.type.is_pyobject:
            code.put_incref(node.result(), node.type, nanny=True)
            code.put_giveref(node.result())

        # generate declarations for any temporaries
        declaration_code = CythonCCodeWriter(self.context, code.minicode)
        declaration_code.put_temp_declarations(code.funcstate)
        self.code.declaration_levels[0].putln(declaration_code.getvalue())
        self.code.putln(code.getvalue())

        return node.result()

class CCodeGenCleanup(codegen.CodeGenCleanup):
    error_handler_level = 0
    def visit_ErrorHandler(self, node):
        self.error_handler_level += 1
        super(CCodeGenCleanup, self).visit_ErrorHandler(node)
        self.error_handler_level -= 1
        if self.error_handler_level == 0:
            self.code.putln("__Pyx_RefNannyFinishContext();")
        return node

    def visit_NodeWrapper(self, node):
        code = create_hybrid_code(self, self.code)
        node.opaque_node.generate_disposal_code(code)
        self.code.putln(code.getvalue())

class CythonGraphvizGenerator(graphviz.GraphvizGenerator):
    """
    Generate a graphviz graph for our Cython AST.
    """

    def __init__(self, context, name):
        super(CythonGraphvizGenerator, self).__init__(context, name)
        self.set_mini_colors()

    def format_node(self, node):
        if isinstance(node, miniast.Node):
            return super(CythonGraphvizGenerator, self).format_node(node)

        result = type(node).__name__
        if isinstance(node, (ExprNodes.BinopNode, ExprNodes.UnopNode)):
            format_value = node.operator
        else:
            return result

        return "%s(%s)" % (result, format_value)

    def set_mini_colors(self):
        self.node_color = 'purple'
        self.edge_color = 'black'
        self.edge_fontcolor = 'black'

    def set_cython_colors(self):
        self.node_color = 'magenta'
        self.edge_color = 'black'
        self.edge_fontcolor = 'black'

    def visit_NodeWrapper(self, node):
        self.node_color = 'red'
        pydot_node = self.create_node(node)
        self.set_cython_colors()

        # monkey patch children so we visit the Cython AST
        # Note: it would be cleaner to start a new transform from this point,
        #       but this works since Context.getchildren() works the same for
        #       minivect and Cython
        node.child_attrs = ['opaque_node']
        result = self.visit_Node(node, pydot_node)
        node.child_attrs = []

        self.set_mini_colors()
        return result

    def visit_OperandNode(self, node):
        self.node_color = 'red'
        pydot_node = self.create_node(node)
        self.set_mini_colors()

        node.subexprs = ['variable']
        result = self.visit_Node(node, pydot_node)
        node.subexprs = []

        self.set_cython_colors()
        return result

class Context(miniast.CContext):

    debug = _context_debug

    codegen_cls = CCodeGen
    cleanup_codegen_cls = CCodeGenCleanup
    specializer_mixin_cls = CythonSpecializerMixin
    graphviz_cls = CythonGraphvizGenerator

    def getchildren(self, node):
        return node.child_attrs

    def declare_type(self, type):
        if type.is_typewrapper:
            return type.opaque_type.declaration_code("")

        return super(Context, self).declare_type(type)

    def may_error(self, node):
        return (node.type.resolve().is_pyobject or
                node.type.resolve().is_complex or
                (node.type.is_memoryviewslice and node.type.dtype.is_pyobject))

    def str_tree(self, node):
        return Visitor.PrintTree()(node)

class CythonCCodeWriter(Code.CCodeWriter):

    def __init__(self, context, minicode):
        super(CythonCCodeWriter, self).__init__()
        self.minicode = minicode
        self.globalstate = context.original_cython_code.globalstate

    def mark_pos(self, pos):
        pass

    def set_error_info(self, pos):
        fn_var, lineno_var, col_var = [
            self.minicode.mangle(v.name)
                for v in self.codegen.function.posinfo.variables]

        filename_idx = self.lookup_filename(pos[0])
        return '*%s = %s[%d]; *%s = %s;' % (
            fn_var, Naming.filetable_cname, filename_idx,
            lineno_var, pos[1])

    def error_goto(self, pos):
        assert self.codegen.error_handlers

        label = self.codegen.error_handlers[-1].error_label
        return "{%s goto %s;}" % (self.set_error_info(pos), label.mangled_name)

    def mangle(self, name):
        "We are simultaneously a mini-CodeWriter and a Cython-CodeWriter"
        return self.minicode.mangle(name)

class OperandNode(ExprNodes.ExprNode):
    """
    The purpose of this node is to wrap a miniast variable and dispatch
    to the miniast code generator from within the Cython code generation
    process.

    This happens when certain operations are not supported natively in
    elementwise expressions, such as operations on complex numbers or
    objects. So the miniast has a NodeWrapper wrapping a Cython AST, of
    which an OperandNode is a leaf, which has to return back again to
    the miniast code generation process.

    Summary:

        miniast
            -> cython ast
                -> operand node
                    -> miniast
    """

    subexprs = []

    def analyse_types(self, env):
        "self.type is already set"

    def generate_result_code(self, code):
        pass

    def result(self):
        return self.codegen.visit(self.variable)


class MemoryAllocationNode(ExprNodes.ExprNode):
    subexprs = ['size']

    def analyse_types(self, env):
        self.is_temp = True
        self.type = PyrexTypes.CPtrType(self.dtype)
        self.size.analyse_types(env)

    def generate_result_code(self, code):
        code.putln("%s = (%s) malloc(%s);" % (self.result(),
                                              self.type.declaration_code(""),
                                              self.size.result()))
        code.putln("if (!%s) {" % self.result())
        if self.in_nogil_context:
            code.put_ensure_gil()
        code.putln(    "PyErr_NoMemory();")
        if self.in_nogil_context:
            code.put_release_ensure_gil()
        code.putln(code.error_goto(self.pos))
        code.putln("}")

    def generate_disposal_code(self, code):
        code.putln("free(%s);" % self.result())
        code.putln("%s = NULL;" % self.result())

class TempSliceMemory(ExprNodes.ExprNode):
    """
    Allocate a temporary memoryview slice with contiguous strides, in the
    order of Cython/Utilities/MemoryView.pyx:get_best_order(dst).

        target   The memoryview slice which we are creating a new contiguous
                 memory region for. Must be a temp.
    """

    subexprs = ['data']

    def analyse_types(self, env):
        # self.type = self.target.type
        self.dtype = self.target.type.dtype
        self.memsize = UtilNodes.ResultRefNode(
                    pos=self.pos, type=PyrexTypes.c_py_ssize_t_type)
        self.data = MemoryAllocationNode(self.pos, dtype=self.dtype,
                                         size=self.memsize)
        self.data.analyse_types(env)

    def generate_evaluation_code(self, code):
        "set the size of memory to allocate before we evaluate subexpressions"
        sizes = ["sizeof(%s)" % self.dtype.declaration_code("")]
        for i in range(self.target.type.ndim):
            sizes.append("%s.shape[%d]" % (self.result(), i))

        self.memsize.result_code = " * ".join(sizes)
        super(TempSliceMemory, self).generate_evaluation_code(code)

    def generate_result_code(self, code):
        "Copy the slice struct and compute the contiguous strides"
        code.putln("%s.data = (char *) %s;" % (self.result(),
                                               self.data.result()))
        order = MemoryView.get_best_slice_order(self.target)
        t = (self.result(), self.result(),
             self.dtype.declaration_code(""),
             self.target.type.ndim, order)

        code.putln("__pyx_fill_contig_strides_array("
                   "&%s.shape[0], &%s.strides[0], sizeof(%s), %d, %s);" % t)

    def result(self):
        return self.target.result()

class CheckOverlappingMemoryNode(ExprNodes.ExprNode):
    subexprs = ['dst']

    def analyse_types(self, env):
        self.type = PyrexTypes.c_bint_type
        self.dst.analyse_types(env)
        self.is_temp = True

    def generate_result_code(self, code):
        # Check for overlapping memory
        dst = self.dst.result()
        dst_ndim = self.dst.type.ndim

        def condition(op):
            f = "__pyx_slices_overlap"
            result = "%s(%s, %s, %d, %s)" % (f, dst, op.result(),
                                             dst_ndim, op.type.ndim)
            if op.type.ndim == dst_ndim:
                f = "__pyx_read_after_write"
                overlap = "%s(%s, %s, %d)" % (f, self.dst.result(),
                                              op.result(), dst_ndim)
                result = "(%s && %s)" % (result, overlap)

            return result

        code.putln("%s = %s;" % (
            self.result(), " || ".join(condition(op) for op in self.operands)))

        if _context_debug:
            code.putln('printf("overlapping memory: %%d\\n", %s);' %
                                                            self.result())

class BroadcastNode(ExprNodes.ExprNode):
    """
    Broadcast the given operands.
        operands:
            All operands we are broadcasting, must be temps
        result():
            Whether the operation is braodcasting in some axis
        max_ndim:
            ndim of the broadcasted operands
    """

    subexprs = []
    init_shape = True

    def analyse_types(self, env):
        self.type = PyrexTypes.c_int_type
        self.is_temp = True

    def init_broadcast_flag(self, code, default=None):
        if default is None:
            default = self.definitely_broadcasting()
        code.putln("%s = %d;" % (self.result(), default))

    def definitely_broadcasting(self):
        return miniutils.any(op.type.ndim != self.max_ndim
                                 for op in self.operands)

    def generate_broadcasting_code(self, code):
        if self.init_shape:
            for i in range(self.max_ndim):
                code.putln("%s.shape[%d] = 1;" % (self.dst_slice.result(), i))

        for operand in self.operands:
            result = operand.result()
            format_tuple = (
                "__pyx_memoryview_broadcast", self.dst_slice.result(),
                result, result, self.max_ndim, operand.type.ndim,
                self.result())
            sig = "%s(&%s.shape[0], &%s.shape[0], &%s.strides[0], %d, %d, &%s)"
            code.putln(code.error_goto_if_neg(sig % format_tuple, self.pos))

    def generate_result_code(self, code):
        """
        The owner of this node should call init_broadcast_flag() and
        generate_broadcasting_code().
        """

def slice_type(type, ndim):
    return PyrexTypes.MemoryViewSliceType(type.dtype, type.axes[-ndim:])

class UnbroadcastDestNode(ExprNodes.ExprNode):
    subexprs = []
    def analyse_types(self, env):
        self.type = slice_type(self.lhs.type, self.rhs.type.ndim)
        self.is_temp = True

    def generate_result_code(self, code):
        pass

class TempSliceStruct(ExprNodes.ExprNode):
    """
    Alloate a temporary memoryview slice (only the struct temporary).
    """
    subexprs = []
    def analyse_types(self, env):
        self.type = PyrexTypes.MemoryViewSliceType(
            self.dtype, self.axes[-self.ndim:])
        self.is_temp = True

    def generate_assignment_code(self, rhs, code):
        code.put_incref_memoryviewslice(rhs.result(),
                                        have_gil=not self.in_nogil_context)
        code.putln("%s = %s;" % (self.result(), rhs.result()))
        rhs.generate_disposal_code(code)
        rhs.free_temps(code)

    def generate_result_code(self, code):
        pass

class TempCythonArrayNode(ExprNodes.ExprNode):
    """
    Attributes:
        dest_array_type
        rhs: the broadcasted rhs node
    """
    subexprs = ['format_string']
    def analyse_types(self, env):
        self.dtype = self.dest_array_type.dtype
        self.type = PyrexTypes.py_object_type
        self.format_string = ExprNodes.FormatStringNode(
                                self.pos, dtype=self.dtype)
        self.format_string.analyse_types(env)
        self.is_temp = True

    def generate_result_code(self, code):
        t = (self.result(), self.rhs.result(), self.rhs.type.ndim,
             self.dtype.declaration_code(""), self.format_string.result(),
             MemoryView.get_best_slice_order(self.rhs))
        code.putln("%s = (PyObject *) __pyx_array_new_simple("
                        "&%s.shape[0], %d, sizeof(%s), %s, %s);" % t)
        code.putln(code.error_goto_if_null(self.result(), self.pos))
        code.put_gotref(self.result())

class DetermineArrayLayoutNode(ExprNodes.ExprNode):
    subexprs = []
    def analyse_types(self, env):
        self.type = PyrexTypes.c_int_type
        self.is_temp = True

    def generate_result_code(self, code):
        memview_struct = MemoryView.memviewslice_cname
        nops = len(self.operands)
        code.begin_block()
        ops = ", ".join("&%s" % op.result() for op in self.operands)
        ndims = ", ".join(str(op.type.ndim) for op in self.operands)
        itemsizes = ", ".join("sizeof(%s)" % op.type.dtype.declaration_code("")
                                  for op in self.operands)

        code.putln("const %s *__pyx_array_ops[%d] = { %s };" % (
                                            memview_struct, nops, ops))
        code.putln("int __pyx_ndims[%d] = { %s };" % (nops, ndims))
        code.putln("Py_ssize_t __pyx_itemsizes[%d] = { %s };" %
                                                    (nops, itemsizes))
        code.putln("%s = __pyx_get_arrays_ordering("
                        "__pyx_array_ops, __pyx_ndims, __pyx_itemsizes, %d);" %
                                                        (self.result(), nops))
        code.end_block()

def all_c_or_f_contig(operands):
    """
    Return whether all operands are contiguous, or have mixed contiguity, as
    well as whether they are C or F contiguous
    """
    all_c_contig = miniutils.all(op.type.is_c_contig for op in operands)
    all_f_contig = miniutils.all(op.type.is_f_contig for op in operands)
    any_c_contig = miniutils.any(op.type.is_c_contig for op in operands)
    any_f_contig = miniutils.any(op.type.is_f_contig for op in operands)

    broadcasting = len(set(op.type.ndim for op in operands)) > 1
    all_c_contig = all_c_contig and not broadcasting
    all_f_contig = all_f_contig and not broadcasting

    return (all_c_contig or all_f_contig, any_c_contig and any_f_contig,
            all_c_contig, all_f_contig)

class SpecializationCaller(ExprNodes.ExprNode):
    """
    Wraps a mapped AST.

    context: Context attribute
    operands: all participating array views
    scalar_operands: non-array operands
    function: miniast function wrapping the array expression

    During code generation:
        broadcasting: result code indicating whether the operation is
                      broadcasting
    """

    subexprs = ['array_layout']
    _code_cache = {}
    code_counter = 0
    target = None

    def analyse_types(self, env):
        self.all_contig = all_c_or_f_contig(self.operands)[0]
        rhs_ndim = max(op.type.ndim for op in self.operands)

        self.array_layout = DetermineArrayLayoutNode(
                            self.pos, operands=[self] + self.operands)
        self.array_layout.analyse_types(env)

        if not self.target:
            if self.dst.type.ndim >= rhs_ndim:
                axes = self.dst.type.axes
            elif self.all_contig:
                types = self.dst.type.c_f_contig_types(rhs_ndim)
                axes = types[all_f_contig].axes
            else:
                axes = [('direct', 'strided')] * rhs_ndim

            self.target = TempSliceStruct(self.pos, ndim=rhs_ndim,
                                          dtype=self.dst.type.dtype,
                                          axes=axes)
            self.target.analyse_types(env)

        self.max_ndim = max(self.dst.type.ndim, self.target.type.ndim)
        self.type = self.target.type

    def align_with_lhs(self, code):
        """
        Remove a broadcasting offset for the RHS and remove that offset. E.g.

            a1d[:] = b2d[:] + 1

        Here b2d is demoted to a 1d array, and shape[0] is asserted to be 1.

        Note: we never broadcast the LHS with the RHS since we only want to
        evaluate the RHS once, and then broadcast the result along the LHS.
        """
        if self.type.ndim <= self.dst.type.ndim:
            return

        code.putln("/* Align RHS with LHS */")
        lhs_offset, rhs_offset = offsets(self.dst, self)
        bound = self.type.ndim - rhs_offset

        if bound > 1:
            i = code.funcstate.allocate_temp(PyrexTypes.c_int_type,
                                             manage_ref=False)
            code.putln("for (%s = 0; %s < %d; %s++) {" % (i, i, bound, i))
        else:
            i = "0"

        t = self.result(), i, self.result(), i, rhs_offset
        code.putln(    "%s.shape[%s] = %s.shape[%s + %d];" % t)
        code.putln(    "%s.strides[%s] = %s.strides[%s + %d];" % t)

        if bound > 1:
            code.putln("}")
            code.funcstate.release_temp(i)

        self.type = self.target.type = slice_type(self.dst.type, self.type.ndim)

    def result(self):
        return self.target.result()

    def run_specializer(self, code, specializer, guard=None, counter=None):
        """
        Run a given minivect specializer and optionally surround the prototype
        and implementation code with a preprocessor guard.
        """
        if graphviz_out_filename:
            global graphviz_counter

            filename = graphviz_out_filename % graphviz_counter
            graphviz_counter += 1

            print "Writing to", filename
            graphviz_outfile = open(filename, 'w')
        else:
            graphviz_outfile = None

        codes = self.context.run(self.function, [specializer],
                                 graphviz_outfile=graphviz_outfile)
        specializer, ast, codewriter, (proto, impl) = iter(codes).next()

        if guard is not None:
            proto = "%s\n%s\n#endif\n" % (guard, proto)
            impl = "%s\n%s\n#endif\n" % (guard, impl)

        if counter is None:
            counter = self.get_function_counter(proto, impl)

        self.put_specialization(code, specializer, ast, codewriter,
                                proto, impl, counter)
        return specializer, ast, codewriter, (proto, impl), counter

    def get_function_counter(self, proto, impl):
        code_counter = self.code_counter
        filename = getattr(self.pos[0], 'filename', None)
        if filename is not None:
            key = self.pos[0].filename, proto, impl
            if key in self._code_cache:
                code_counter = self._code_cache[key]
            else:
                self._code_cache[key] = code_counter
                SpecializationCaller.code_counter += 1
        else:
            SpecializationCaller.code_counter += 1

        return code_counter

    def put_specialization(self, code, specializer, specialized_function,
                           codewriter, proto, impl, code_counter):
        "Insert generated minivect code into the Cython module"
        # print id(specialized_function), specialized_function.mangled_name
        specialized_function.mangled_name = (
                    specialized_function.mangled_name % (code_counter,))
        proto = proto % code_counter
        impl = impl % ((code_counter,) * impl.count("%d"))

        utility = Code.UtilityCode(proto=proto, impl=impl)
        code.globalstate.use_utility_code(utility)

        if _debug:
            marker =  '-' * 20
            print marker, 'proto', marker
            print proto
            print marker, 'impl', marker
            print impl

    def put_specialization_and_call(self, code, specializer, guard=None,
                                    counter=None, can_vectorize=True):
        "Generate code, inject it into the Cython module and generate a call"
        if specializer.vectorized_equivalents and can_vectorize:
            self.put_vectorized_specializations(code, specializer)
        else:
            # Generate code
            result = self.run_specializer(code, specializer, guard, counter)
            # Generate call to the generated code
            self.put_specialized_call(code, *result)

    def put_vectorized_specializations(self, code, normal_specializer):
        """
        Generate several versions of the code, one for SSE*, one for AVX
        and one unvectorized version.
        """
        sse_specializer, avx_specializer = (
                                normal_specializer.vectorized_equivalents)

        can_vectorize = sse_specializer.can_vectorize(self.context,
                                                      self.function)

        if can_vectorize:
            guard = "#if %s == %%d" % cython_vector_size
            sse_size = sse_specializer.vector_size
            avx_size = avx_specializer.vector_size
            sse_guard = guard % sse_size
            avx_guard = guard % avx_size
            normal_guard = "#if !(%s == %d || %s == %d)" % (
                    cython_vector_size, sse_size, cython_vector_size, avx_size)

            _, _, _, _, counter = self.run_specializer(
                            code, normal_specializer, guard=normal_guard)
            self.run_specializer(code, sse_specializer, guard=sse_guard,
                                 counter=counter)
            self.put_specialization_and_call(code, avx_specializer,
                                             guard=avx_guard, counter=counter)
        else:
            self.put_specialization_and_call(code, normal_specializer,
                                             can_vectorize=False)

    def is_c_order_code(self):
        return "%s & __PYX_ARRAY_C_ORDER" % self.array_layout.result()

    def put_ordered_specializations(self, code, c_specialization,
                                                f_specialization):
        code.putln("if (%s) {" % self.is_c_order_code())
        self.put_specialization_and_call(code, c_specialization)
        code.putln("} else {")
        self.put_specialization_and_call(code, f_specialization)
        code.putln("}")

    def _put_contig_specialization(self, code, if_clause, contig, mixed_contig):
        if not mixed_contig:
            code.putln("/* Contiguous specialization */")
            not_broadcasting = "!%s" % self.broadcasting
            if contig:
                condition = not_broadcasting
            else:
                condition = "%s & __PYX_ARRAYS_ARE_CONTIG && %s" % (
                    self.array_layout.result(), not_broadcasting)

            code.putln("%s (%s) {" % (if_clause, condition))

            self.put_specialization_and_call(code,
                                             specializers.ContigSpecializer)

            code.putln("}")
            if_clause = "else if"

        return if_clause

    def _put_tiled_specialization(self, code, if_clause, mixed_contig):
        if not mixed_contig:
            code.putln("%s (%s & (__PYX_ARRAYS_ARE_MIXED_CONTIG|"
                                 "__PYX_ARRAYS_ARE_MIXED_STRIDED)) {" %
                                 (if_clause, self.array_layout.result()))

        code.putln("/* Tiled specializations */")
        self.put_ordered_specializations(code,
             specializers.CTiledStridedSpecializer,
             specializers.FTiledStridedSpecializer)

        if not mixed_contig:
            code.putln("}")

        return if_clause

    def _put_inner_contig_specializations(self, code, if_clause, mixed_contig):
        """
        Insert the inner contiguous specialization, if we have more than two
        dimensions. We the rhs is 2D but the LHS 1D, it means the actual
        pattern is 1D.
        """
        if (not mixed_contig and self.dst.type.ndim > 1 and
                self.target.type.ndim > 1):
            code.putln("%s (%s & __PYX_ARRAYS_ARE_INNER_CONTIG) {" %
                                    (if_clause, self.array_layout.result()))
            self.put_ordered_specializations(code,
                    specializers.StridedCInnerContigSpecializer,
                    specializers.StridedFortranInnerContigSpecializer)
            code.putln("}")
            if_clause = "else if"

        return if_clause

    def _put_strided_specializations(self, code, if_clause, mixed_contig):
        if mixed_contig:
            return

        if if_clause != "if":
            code.putln("else {")

        code.putln("/* Strided specializations */")
        self.put_ordered_specializations(code, specializers.StridedSpecializer,
                                         specializers.StridedFortranSpecializer)

        if if_clause != "if":
            code.putln("}")

    def generate_result_code(self, code):
        contig, mixed_contig, c_contig, f_contig = all_c_or_f_contig(self.operands)

        self.context.original_cython_code = code

        if_clause = "if"
        if_clause = self._put_contig_specialization(code, if_clause,
                                                    contig, mixed_contig)
        if self.target.type.ndim > 1:
            if not c_contig and not f_contig:
                if_clause = self._put_tiled_specialization(code, if_clause,
                                                           mixed_contig)
            if_clause = self._put_inner_contig_specializations(code, if_clause,
                                                               mixed_contig)
        self._put_strided_specializations(code, if_clause, mixed_contig)

    def contig_condition(self, specializer):
        if specializer.is_contig_specializer:
            if not self.all_contig:
                # todo: implement a memoryview flag to quickly check whether
                #       it is contig for each operand
                return "0"
            return "!%s" % self.broadcasting

        return MemoryView.get_best_slice_order(self.target)

    def put_specialized_call(self, code, specializer, specialized_function,
                             codewriter, result_code, code_counter=None):
        """
        Generate a call to a given specializer and specialized
        minivect function.
        """
        # all function call arguments
        offset = max(self.target.type.ndim - self.function.ndim, 0)
        args = ["&%s.shape[%d]" % (self.result(), offset)]

        if specialized_function.posinfo:
            args.extend(["&%s" % Naming.filename_cname,
                         "&%s" % Naming.lineno_cname,
                         "NULL"])

        for operand in [self] + self.operands:
            result = operand.result()
            if operand.type.is_memoryviewslice:
                dtype_pointer_decl = operand.type.dtype.declaration_code("")
                args.append('(%s *) %s.data' % (dtype_pointer_decl, result))
                if not specializer.is_contig_specializer:
                    offset = max(operand.type.ndim - self.function.ndim, 0)
                    args.append("&%s.strides[%d]" % (result, offset))
            else:
                args.append(result)

        args.extend(scalar_arg.result() for scalar_arg in self.scalar_operands)

        n_operands = len(self.operands)
        if specializer.is_tiled_specializer:
            dtype_decl = self.type.dtype.declaration_code("")
            args.append("__pyx_vector_get_tile_size(sizeof(%s), %d)" % (
                                                dtype_decl, n_operands))
        args.append("__pyx_vector_get_omp_size(%d)" % n_operands)

        call = "%s(%s)" % (specialized_function.mangled_name, ", ".join(args))

        if self.may_error:
            lbl = code.funcstate.error_label
            code.funcstate.use_label(lbl)
            code.putln("if (unlikely(%s < 0)) { goto %s; }" % (call, lbl))
        else:
            code.putln("(void) %s;" % call)

def offsets(lhs, rhs):
    lhs_ndim = lhs.type.ndim
    rhs_ndim = rhs.type.ndim
    lhs_offset = max(lhs_ndim - rhs_ndim, 0)
    rhs_offset = max(rhs_ndim - lhs_ndim, 0)
    return lhs_offset, rhs_offset

class ElementalNode(ExprNodes.ExprNode):
    """
    Evaluate the expression on the right hand side before assigning to the
    expression on the left hand side. This is needed in two situations:

        1) The rhs has overlapping memory with the lhs, and executing the
           expression would write to memory of the lhs before it would be
           read
        2) Some error may occur while evaluating the rhs

    Attributes:
        rhs: entire RHS expression node
        sources: list of broadcastable array operands, excluding the LHS
        may_error: indicates whether the expression may raise a sudden error

    For expressions like 'c = a + b', i.e., in case of no slice assignment
        acquire_slice:
            This assignment creates a cython.view.array and acquires a
            memoryview slice from that in self.lhs (a temporary)
    """

    subexprs = ['operands', 'scalar_operands', 'temp_nodes', 'lhs',
                'check_overlap', 'rhs', 'final_assignment_node',
                'broadcast', 'final_broadcast', 'temp_dst',
                'acquire_slice', 'final_lhs_assignment']

    check_overlap = None
    temp_dst = None
    may_error = None
    rhs = None
    rhs_target = None
    final_broadcast = None
    final_assignment_node = None

    acquire_slice = None
    final_lhs_assignment = None

    def analyse_expressions(self, env):
        self.temp_nodes = []
        self.max_ndim = max(op.type.ndim for op in self.operands)

        if isinstance(self.lhs, UnbroadcastDestNode):
            self.lhs = self.lhs.lhs

        self.lhs = self.lhs.coerce_to_simple(env)

        self.rhs = SpecializationCaller(
            self.operands[0].pos, context=self.minicontext,
            dst=self.lhs, operands=self.operands,
            scalar_operands=self.scalar_operands,
            function=self.rhs_function,
            may_error=self.may_error)
        self.rhs.analyse_types(env)

        self.temp_nodes.append(self.rhs.target)

        for i, operand in enumerate(self.operands):
            operand.analyse_types(env)

        if not self.acquire_slice:
            self.check_overlap = CheckOverlappingMemoryNode(
                        self.pos, dst=self.lhs.wrap_in_clone_node(),
                        operands=self.operands)
            self.check_overlap.analyse_types(env)

            self.final_assignment_node = self.final_assignment()
            self.final_assignment_node.analyse_types(env)
            self.temp_nodes.append(self.final_assignment_node.target)

            self.final_broadcast = BroadcastNode(
                self.pos, operands=[self.rhs], max_ndim=self.lhs.type.ndim,
                dst_slice=self.lhs, init_shape=False)
            self.final_broadcast.analyse_types(env)

            self.temp_dst = TempSliceMemory(self.rhs.pos, target=self.rhs)
            self.temp_dst.analyse_types(env)

        self.broadcast = BroadcastNode(self.pos,
                                       operands=self.operands,
                                       max_ndim=self.max_ndim,
                                       dst_slice=self.rhs)
        self.broadcast.analyse_types(env)

    def final_assignment(self):
        b = self.minicontext.astbuilder
        typemapper = self.minicontext.typemapper

        lhs_offset, rhs_offset = offsets(self.lhs, self.rhs)
        rhs_type = PyrexTypes.MemoryViewSliceType(
            self.rhs.type.dtype, self.rhs.type.axes[rhs_offset:])

        lhs_var = b.variable(typemapper.map_type(self.lhs.type, wrap=True), 'lhs')
        rhs_var = b.variable(typemapper.map_type(rhs_type, wrap=True), 'rhs')

        if self.lhs.type.dtype.is_pyobject:
            rhs_tmp = b.temp(rhs_var.type.dtype)
            body = b.stats(b.assign(rhs_tmp, rhs_var),
                           b.decref(lhs_var),
                           b.incref(rhs_tmp),
                           b.assign(lhs_var, rhs_tmp))
        else:
            body = b.assign(lhs_var, rhs_var)

        args = [b.array_funcarg(lhs_var), b.array_funcarg(rhs_var)]
        func = b.function('final_assignment%d', body, args)
        return SpecializationCaller(self.pos, context=self.minicontext,
                                    operands=[self.rhs], function=func,
                                    scalar_operands=[],
                                    dst=self.lhs, may_error=False,
                                    target=self.lhs.wrap_in_clone_node())

    def overlap(self):
        if self.may_error:
            return "1"
        return "unlikely(%s)" % self.check_overlap.result()

    def init_rhs_temp(self, code):
        """
        In case of no overlapping memory, assign directly to the LHS.
        """
        code.putln("%s.data = %s.data;" % (self.rhs.result(), self.lhs.result()))
        lhs_offset, rhs_offset = offsets(self.lhs, self.rhs)
        for i in range(self.rhs.type.ndim):
            code.putln("%s.strides[%d] = %s.strides[%d];" % (
                self.rhs.result(), i, self.lhs.result(), i + lhs_offset))

    def advance_lhs_data_ptr(self, code):
        """
        If we performed a direct assignment to the LHS, but the RHS was
        broadcasting, perform the final broadcasting assignment by advancing
        the data pointer of LHS. E.g.

            m3[:, :] = m1[:] * m2[:]

        m3[0, :] contains the data, which we need to broadcast over m3[1:, :]

        (This only works for one dimension at a time, so this would need a
         wrapping loop. Currently disabled, it broadcasts itself with itself).
        """
        lhs_offset, _ = offsets(self.lhs, self.rhs)
        lhs_r, rhs_r = self.lhs.result(), self.rhs.result()

        def advance(i):
            #code.putln("%s.data += %s.strides[%d];" % (lhs_r, lhs_r, i))
            #code.putln("%s.shape[%d] -= 1;" % (lhs_r, i))
            if not lhs_offset:
                self.final_broadcast.init_broadcast_flag(code, True)

        for i in range(lhs_offset):
            advance(i)
            self.final_broadcast.init_broadcast_flag(code, True)

        if not lhs_offset:
            for i in range(self.rhs.type.ndim):
                code.putln("if (%s.shape[%d] > 1 && %s.shape[%d] == 1) {" %
                           (lhs_r, i + lhs_offset, rhs_r, i))
                advance(i + lhs_offset)
                code.putln("}")

    def verify_final_shape(self, code):
        call = "__pyx_verify_shapes(%s, %s, %d, %d)" % (
            self.lhs.result(), self.rhs.result(),
            self.lhs.type.ndim, self.rhs.type.ndim)
        code.putln(code.error_goto_if_neg(call, self.pos))

    def generate_evaluation_code(self, code):
        code.mark_pos(self.pos)

        code.putln("/* LHS */")
        self.lhs.generate_evaluation_code(code)
        self.rhs.target.generate_evaluation_code(code)

        code.putln("/* Evaluate operands */")
        for op in self.operands:
            op.generate_evaluation_code(code)

        for scalar_op in self.scalar_operands:
            scalar_op.generate_evaluation_code(code)

        if self.check_overlap:
            code.putln("/* Check overlapping memory */")
            self.check_overlap.generate_evaluation_code(code)

        code.putln("/* Broadcast all operands in RHS expression */")
        # create and initialize broadcasting flag
        self.broadcast.generate_evaluation_code(code)
        self.broadcast.init_broadcast_flag(code)
        self.broadcast.generate_broadcasting_code(code)

        if not self.acquire_slice:
            self.verify_final_shape(code)
            self.rhs.align_with_lhs(code)
            # Set rhs.data and rhs.strides
            code.putln("/* Allocate scratch space if needed */")
            code.putln("if (%s) {" % self.overlap())
            self.temp_dst.generate_evaluation_code(code)
            code.put("} else {")
            # shut up compiler warnings
            code.putln("%s = NULL;" % self.temp_dst.data.result())
            self.init_rhs_temp(code)
            code.putln("}")
        else:
            self.acquire_slice.generate_execution_code(code)
            self.init_rhs_temp(code)

        code.putln("/* Evaluate expression */")
        self.rhs.broadcasting = self.broadcast.result()
        self.rhs.generate_evaluation_code(code)

        if not self.acquire_slice:
            self.final_broadcast.generate_evaluation_code(code)
            self.final_broadcast.init_broadcast_flag(code)
            code.putln("if (!%s) {" % self.overlap())
            self.advance_lhs_data_ptr(code)
            code.putln("}")

            code.putln("/* Broadcast final RHS and LHS */")
            self.final_assignment_node.target.generate_evaluation_code(code)
            self.final_broadcast.generate_broadcasting_code(code)

            code.putln("/* Final broadcasting assignment */")
            if self.lhs.type.ndim == self.rhs.type.ndim:
                code.putln("if (%s || %s) {" % (self.final_broadcast.result(),
                                                self.overlap()))
            # self.remove_rhs_offset(code)
            self.final_assignment_node.broadcasting = self.final_broadcast.result()
            self.final_assignment_node.generate_evaluation_code(code)
            if self.lhs.type.ndim == self.rhs.type.ndim:
                code.putln("}")

            code.putln("/* Cleanup */")
            code.putln("if (%s) {" % self.overlap())
            self.temp_dst.generate_disposal_code(code)
            self.temp_dst.free_temps(code)
            self.temp_dst = self.temp_dst.wrap_in_clone_node()
            code.putln("}")

    def generate_disposal_code(self, code):
        for child_attr in self.child_attrs:
            if child_attr == 'acquire_slice':
                continue

            value_list = getattr(self, child_attr)
            if not isinstance(value_list, list):
                value_list = [value_list]

            for node in value_list:
                if node:
                    node.generate_disposal_code(code)
                    node.free_temps(code)

    def free_temps(self, code):
        "We already released temps during disposal code generation."

    def calculate_result_code(self):
        return ""

class ElementalNodeWrapper(ExprNodes.ExprNode):
    """
    This node is used in case of no slice assignment.

    Attributes:
        elemental_node:
            The ElementalNode being wrapped
        slice_result:
            The expression holding the final memoryview slice with the
            result.
    """
    subexprs = ['elemental_node']
    def analyse_types(self, env):
        self.type = self.slice_result.type

    def generate_assignment_code(self, rhs, code):
        self.slice_result.generate_assignment_code(self, rhs, code)

    def generate_result_code(self, code):
        pass

    def result(self):
        return self.slice_result.result()

def need_wrapper_node(node):
    """
    Return whether a Cython node that needs to be mapped to a miniast Node,
    should be mapped or wrapped (i.e., should minivect or Cython generate
    the code to evaluate the expression?).
    """
    type = node.type
    while True:
        if type.is_ptr:
            type = type.base_type
        elif type.is_memoryviewslice:
            type = type.dtype
        else:
            break

    type = type.resolve()
    return type.is_pyobject or type.is_complex

def get_dtype(type):
    if type.is_memoryviewslice:
        return type.dtype
    return type

class CythonASTInMiniastTransform(Visitor.VisitorTransform):

    def __init__(self, env):
        super(CythonASTInMiniastTransform, self).__init__()
        self.env = env
        self.operands = []

    def visit_UnopNode(self, node):
        dtype = get_dtype(node.type)
        node = type(node)(node.pos, type=dtype, operator=node.operator,
                          operand=self.visit(node.operand))
        node.analyse_types(self.env)
        return node

    def visit_BinopNode(self, node):
        dtype = get_dtype(node.type)
        node = type(node)(node.pos, type=dtype, operator=node.operator,
                          operand1=self.visit(node.operand1),
                          operand2=self.visit(node.operand2))
        node.analyse_types(self.env)
        return node

    def visit_ExprNode(self, node):
        node = OperandNode(node.pos, type=get_dtype(node.type), node=node)
        self.operands.append(node)
        return node

def elemental_dispatcher(f):
    def wrapper_method(self, node):
        if not node.is_elemental:
            return self.register_operand(node)

        minitype = self.map_type(node, wrap=True)
        if need_wrapper_node(node):
            self.may_error = True
            return self.register_wrapper_node(node)

        return f(self, node, minitype)

    wrapper_method.__name__ = f.__name__
    wrapper_method.__doc__ = f.__doc__
    return wrapper_method

def resolve_node(node):
    # Only resolve rhs operands
    #if isinstance(node, UnbroadcastDestNode):
    #    return resolve_node(node.lhs)
    if isinstance(node, ExprNodes.CoerceToTempNode):
        return resolve_node(node.arg)
    elif node.is_memview_slice and node.is_ellipsis_noop:
        return resolve_node(node.base)
    else:
        return node

def equal_operands(node1, node2):
    node1 = resolve_node(node1)
    node2 = resolve_node(node2)

    if node1.is_name and node2.is_name and node1.name == node2.name:
        return True
    elif node1.is_memview_slice and node2.is_memview_slice:
        return Utils.all(
            equal_operands(index1, index2)
                for index1, index2 in zip(node1.indices, node2.indices))
    elif node1.is_literal and node2.is_literal:
        return node1.value == node2.value
    elif node1.is_none and node2.is_none:
        return True

    return False

class ElementalMapper(specializers.ASTMapper):
    """
    When some elementwise expression is found in the Cython AST, convert that
    tree to a minivect AST.
    """

    wrapping = 0

    def __init__(self, context, env, max_ndim):
        super(ElementalMapper, self).__init__(context)
        self.env = env
        # operands to the function in callee space
        self.operands = []
        # scalar operands to the function in callee space
        self.scalar_operands = []

        # miniast function arguments to the function
        self.funcargs = []
        self.error = False
        self.may_error = False
        self.max_ndim = max_ndim

    def map_type(self, node, **kwds):
        try:
            return super(ElementalMapper, self).map_type(node, **kwds)
        except minierror.UnmappableTypeError, e:
            error(node.pos, "Unsupported type in elementwise "
                            "operation: %s" % (node.type,))
            raise

    def register_operand(self, node):
        """
        Register a non-elemental subexpression, and pass it in to the function
        we are generating as an argument.
        """
        assert not node.is_elemental

        b = self.astbuilder

        for i, seen_operand in enumerate(self.operands):
            if equal_operands(seen_operand, node):
                return self.funcargs[i].variable

        minitype = self.map_type(node, wrap=True)
        if node.type.is_memoryviewslice:
            node = node.coerce_to_temp(self.env)
            self.operands.append(node)
        elif node.is_literal:
            return b.constant(node.value, type=minitype)
        else:
            node = node.coerce_to_simple(self.env)
            self.scalar_operands.append(node)

        varname = '__pyx_op%d' % (len(self.operands) + len(self.scalar_operands))
        if node.type.is_memoryviewslice:
            funcarg = b.array_funcarg(b.variable(minitype, varname))
            funcarg.type.ndim = min(funcarg.type.ndim, self.max_ndim)
        else:
            funcarg = b.funcarg(b.variable(minitype, varname))

        self.funcargs.append(funcarg)
        return funcarg.variable

    def register_wrapper_node(self, node):
        """
        Create a miniast.NodeWrapper for functionality that Cython provides,
        but that we want to use inside miniast expressions.
        """
        assert node.is_elemental

        transform = CythonASTInMiniastTransform(self.env)
        try:
            node = transform.visit(node)
        except CompileError, e:
            error(e.position, e.message_only)
            return None

        for operand in transform.operands:
            operand.variable = self.register_operand(operand.node)

        def specialize_node(nodewrapper, memo):
            return copy.deepcopy(node, memo)

        return self.astbuilder.wrap(node, specialize_node,
                                    operands=transform.operands)

    def visit_ExprNode(self, node):
        """
        Some expression which cannot be converted to a miniast, but is passed
        in as an argument to the function generated from the miniast.
        """
        return self.register_operand(node)

    def visit_SingleAssignmentNode(self, node):
        if isinstance(node.lhs, ExprNodes.MemoryCopySlice):
            lhs = node.lhs.dst
        else:
            lhs = node.lhs
        return self.astbuilder.assign(self.visit(lhs),
                                      self.visit(node.rhs))

    @elemental_dispatcher
    def visit_SimpleCallNode(self, node, minitype):
        b = self.astbuilder
        miniargs = []
        elemental_args = set(node.elemental_args)
        for arg in node.args:
            if arg in elemental_args:
                # elementwise argument
                miniargs.append(self.visit(arg))
            else:
                # normal function argument, create partial function
                miniargs.append(self.register_operand(arg))

        minifunc = b.funcname(minitype, node.function.entry.cname)
        return b.funccall(minifunc, miniargs)

    @elemental_dispatcher
    def visit_UnopNode(self, node, minitype):
        return self.astbuilder.unop(minitype, node.operator,
                                    self.visit(node.operand))

    @elemental_dispatcher
    def visit_BinopNode(self, node, minitype):
        op1 = self.visit(node.operand1)
        op2 = self.visit(node.operand2)
        return self.astbuilder.binop(minitype, node.operator, op1, op2)

class ElementWiseOperationsTransform(Visitor.EnvTransform):
    """
    Find elementwise expressions and run ElementalMapper to turn it into
    a minivect AST. Our Cython tree ends here in an ElementalNode, which
    responsibility is to call the function generated by minivect (as well
    as to perform broadcasting and selection of the right specialization).
    """

    in_elemental = 0
    may_error = False

    def visit_ModuleNode(self, node):
        self.minicontext = Context()
        self.minicontext.typemapper = TypeMapper(self.minicontext)
        self.visitchildren(node)
        return node

    def visit_elemental(self, node, lhs=None, acquire_slice=None):
        self.in_elemental += 1
        self.visitchildren(node)
        self.in_elemental -= 1

        if not self.in_elemental:
            # Convert the Cython AST to a minivect AST and generate code
            # to select the right specialization
            load_utilities(self.current_env())

            b = self.minicontext.astbuilder
            b.pos = node.pos
            astmapper = ElementalMapper(self.minicontext, self.current_env(),
                                        max_ndim=lhs.type.ndim)

            try:
                body = astmapper.visit(node)
            except minierror.UnmappableTypeError:
                return None

            name = '__pyx_array_expression%d'

            if astmapper.may_error:
                pos_args = (
                    b.variable(minitypes.c_string_type.pointer(), 'filename'),
                    b.variable(minitypes.int_.pointer(), 'lineno'),
                    b.variable(minitypes.int_.pointer(), 'column'))
                posinfo = b.funcarg(b.variable(None, 'position'), *pos_args)
                self.may_error = False
            else:
                posinfo = None

            function = b.function(name, body, astmapper.funcargs,
                                  posinfo=posinfo)

            if _debug:
                f = open(graphviz_out_filename_unspecialized, 'w')
                f.write(self.minicontext.graphviz(function))
                f.close()

            astmapper.operands.pop(0)

            node = ElementalNode(node.pos,
                                 operands=astmapper.operands,
                                 scalar_operands=astmapper.scalar_operands,
                                 rhs_function=function,
                                 minicontext=self.minicontext,
                                 lhs=lhs,
                                 acquire_slice=acquire_slice)
            node.analyse_expressions(self.current_env())
            #node = Nodes.ExprStatNode(node.pos, expr=node)
        return node

    def visit_SingleAssignmentNode(self, node):
        env = self.current_env()
        if (isinstance(node.lhs, ExprNodes.MemoryCopySlice) and
                node.lhs.is_elemental):
            assert not self.in_elemental
            node.is_elemental = True

            node.lhs.dst = UnbroadcastDestNode(
                node.pos, lhs=node.lhs.dst.coerce_to_temp(env),
                rhs=node.rhs)
            node.lhs.dst.analyse_types(env)

            elemental_node = self.visit_elemental(node, node.lhs.dst)
            return Nodes.ExprStatNode(node.pos, expr=elemental_node)
        else:
            is_elemental = node.rhs.is_elemental
            self.visitchildren(node)
            if is_elemental:
                node.analyse_types(env)
            return node

    def visit_ExprNode(self, node):
        if node.is_elemental:
            if self.in_elemental:
                # We are already in an elemental expression, just recursve
                return self.visit_elemental(node)

            # We are an outer expression which is not a direct assignment
            # We have to create a new array to store the result of the
            # expression, and convert to a minivect AST
            env = self.current_env()
            tmp_lhs, elemental_node = self._create_new_array_node(node, env)
            result = ElementalNodeWrapper(node.pos, slice_result=tmp_lhs,
                                          elemental_node=elemental_node)
            result.analyse_types(env)
            return result

        self.visitchildren(node)
        return node

    def _acquire_new_memview(self, rhs, lazy_rhs, env):
        """
        Acquire a new memoryview slice in a temp.
        The rhs is the broadcasted rhs expression (which is a ProxyNode since
        we don't have its result yet).
        """
        type = rhs.type

        # cyarray = <dtype[:broadcasted_shape[0]]> malloc(broadcasted_shape[0])
        temp_cy_array = TempCythonArrayNode(
                            rhs.pos, dest_array_type=type, rhs=lazy_rhs)

        # cdef dtype[:] tmp
        tmp_lhs = TempSliceStruct(
            rhs.pos, dtype=type.dtype, axes=type.axes, ndim=type.ndim)
        tmp_lhs.analyse_types(env)
        tmp_lhs = tmp_lhs.wrap_in_clone_node()

        # tmp = cyarray
        acquire_assignment = Nodes.SingleAssignmentNode(
            rhs.pos, lhs=tmp_lhs.arg, rhs=temp_cy_array)
        acquire_assignment.analyse_expressions(env)

        return tmp_lhs, acquire_assignment

    def _create_new_array_node(self, rhs, env):
        """
        In an assignment like 'b = a + a', we need to create a new array 'b'.
        We create a cython.view.array, and assign it to variable 'b'.

        Todo: implement temporary memoryview slices without needing the GIL!
        """
        lazy_rhs = ExprNodes.ProxyNode(rhs)
        tmp_lhs, acquire_assignment = self._acquire_new_memview(
                                                rhs, lazy_rhs, env)

        # create elemental assignment
        elemental_assignment = Nodes.SingleAssignmentNode(
                                rhs.pos, lhs=tmp_lhs.arg, rhs=rhs)

        elemental_node = self.visit_elemental(elemental_assignment,
                                              lhs=tmp_lhs.arg,
                                              acquire_slice=acquire_assignment)

        lazy_rhs.arg = elemental_node.rhs
        lazy_rhs.proxy_type()
        #elemental_node.final_lhs_assignment = final_assignment
        return tmp_lhs, elemental_node

def load_utilities(env):
    from Cython.Compiler import CythonScope
    cython_scope = CythonScope.get_cython_scope(env)
    broadcast_utility.declare_in_scope(cython_scope.viewscope,
                                       cython_scope=cython_scope, used=True)

    env.use_utility_code(overlap_utility)
    env.use_utility_code(array_order_utility)
    env.use_utility_code(restrict_utility)
    env.use_utility_code(tile_size_utility)
    env.use_utility_code(vector_size_utility)
    env.use_utility_code(vector_header_utility)

def load_vector_utility(name, context, **kwargs):
    return Code.TempitaUtilityCode.load(name, "Vector.c", context=context, **kwargs)

def load_vector_cy_utility(name, context, **kwargs):
    return UtilityCode.CythonUtilityCode.load(
                name, "Vector.pyx", context=context,
                prefix='__pyx_vector_', **kwargs)

context = dict(MemoryView.context, cython_vector_size=cython_vector_size)

broadcast_utility = MemoryView.load_memview_cy_utility("Broadcasting",
                                                       context=context)
MemoryView.view_utility_code.requires.append(broadcast_utility)
overlap_utility = MemoryView.load_memview_c_utility("ReadAfterWrite",
                                                    context=context)
array_order_utility = load_vector_utility("GetOrder", context)
restrict_utility = load_vector_utility(
    "RestrictUtility", context, proto_block='utility_code_proto_before_types')
omp_size_utility = load_vector_utility("OpenMPAutoTune", context)
tile_size_utility = load_vector_cy_utility("GetTileSize", context,
                                           requires=[omp_size_utility])
vector_size_utility = load_vector_utility(
            "VectorizedUtility", context=context,
            proto_block='utility_code_proto_before_types')

vector_header_utility = load_vector_utility("VectorHeaderUtility",
                                            context=context)